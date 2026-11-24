"""Chat tools (rag/chat/tools.py) and the OpenAI / Gemini tool-calling loops."""
import json
import types

import pytest

import test_rag                                    # installs the faiss stand-in
from test_rag import RecordingLLM, fake_embedder
from fluxdiff.rag import providers
from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.chat.tools import MAX_RESULT_CHARS, HistoryTools
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.llm import llm_client
from fluxdiff.rag.llm.llm_client import LLMBusy, LLMClient
from fluxdiff.rag.retrieval.retriever import Retriever
from fluxdiff.rag.schemas import RAGDocument


def summary(commit, date, message, counts="  component changes : 1\n  net changes       : 0"):
    return RAGDocument(
        content=f"Commit: {commit}\nMessage: {message}\nAuthor: ann\nDate: {date}\n"
                f"File: board.kicad_pcb\nSummary:\n{counts}",
        metadata={"commit": commit, "type": "summary", "file": "board.kicad_pcb",
                  "author": "ann", "date": date})


def section(commit, kind, lines):
    return RAGDocument(
        content=f"Commit: {commit}\nType: {kind}\nFile: board.kicad_pcb\nChanges:\n" + "\n".join(lines),
        metadata={"commit": commit, "type": kind, "file": "board.kicad_pcb"})


A, B, C = "a" * 40, "b" * 40, "c" * 40
DOCS = [
    summary(A, "2022-01-10T10:00:00Z", "Initial layout"),
    summary(C, "2022-03-15T09:00:00+00:00", "Added the 0R resistor to USB shield"),
    summary(B, "2022-02-01T12:00:00+05:30", "Swap charger"),
    section(A, "component", ["Component added: R1", "Component added: R10"]),
    section(B, "component", ["Component value changed: U6 [MCP73831-2-OT] -> [MCP73831T-2ACI/OT]",
                             "Component moved: R10 (1,2) -> (3,4)"]),
    section(C, "net", ["CRITICAL: J1 pad S1 changed from GND -> Net-(J1-PadS1)",
                       "INFO: R5 pad 1 connected to Net-(J1-PadS1)",
                       "ERC NEW: WARNING: Net 'X' is floating (only one real connection: R5)",
                       "ERC FIXED: WARNING: Net 'Y' is floating (only one real connection: R1)"]),
    section(C, "routing", ["Net USB_D+: 3 segments added (2.8 mm on B.Cu; width 0.152 mm)"]),
]


def tools(**kw):
    return HistoryTools(DOCS, **kw)


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def test_list_commits_newest_first_across_timezones():
    out = tools().call("list_commits", {"limit": 2})
    lines = out.splitlines()
    assert lines[0].startswith("3 indexed commit(s)")
    assert lines[1].startswith("- ccccccc | 2022-03-15") and lines[2].startswith("- bbbbbbb | 2022-02-01")
    oldest = tools().call("list_commits", {"oldest_first": True}).splitlines()[1]
    assert oldest.startswith("- aaaaaaa")


def test_get_commit_by_prefix():
    t = tools()
    out = t.call("get_commit", {"commit": "cccc"})
    assert "Added the 0R resistor" in out and "USB_D+" in out and t.touched == [C]
    assert "No indexed commit" in t.call("get_commit", {"commit": "abc"})


def test_component_history_matches_whole_references():
    out = tools().call("component_history", {"ref": "r1"})
    assert "Component added: R1" in out
    assert "R10" not in out.replace("Component added: R1\n", "")      # R1 must not match R10
    assert "floating (only one real connection: R1)" in out
    u6 = tools().call("component_history", {"ref": "U6"})
    assert "bbbbbbb (2022-02-01) Swap charger" in u6 and "MCP73831T-2ACI/OT" in u6


def test_findings_by_status_and_severity():
    t = tools()
    new = t.call("get_findings", {})
    assert "[NEW CRITICAL] (pad net change) J1 pad S1" in new
    assert "[NEW WARNING] (net) Net 'X' is floating" in new
    assert "R5 pad 1 connected" not in new                 # routine INFO pad lines hidden
    assert "Net 'Y'" not in new
    assert "Net 'Y'" in t.call("get_findings", {"status": "FIXED"})
    assert "R5 pad 1 connected" in t.call("get_findings", {"severity": "INFO"})
    assert t.call("get_findings", {"severity": "CRITICAL", "status": "FIXED"}).startswith("No FIXED CRITICAL")


def test_search_changes_is_case_insensitive_and_filterable():
    t = tools()
    assert "USB_D+" in t.call("search_changes", {"text": "usb_d+"})
    assert "No recorded change" in t.call("search_changes", {"text": "usb_d+", "change_type": "net"})


def test_repo_overview_uses_readme():
    t = tools(repo={"label": "acme/board", "kind": "github", "location": "acme/board", "branch": "dev"},
              fetch_readme=lambda loc, br: f"# {loc} on {br}\nA tiny test board.")
    out = t.call("repo_overview", {})
    assert "acme/board" in out and "3 board commit(s) from 2022-01-10 to 2022-03-15" in out
    assert "A tiny test board." in out


def test_unknown_tool_bad_args_and_truncation():
    t = tools()
    assert "Unknown tool" in t.call("rm_rf", {})
    assert "Bad arguments" in t.call("get_commit", {"nope": 1})
    huge = HistoryTools([section(A, "routing", [f"Net N{i}: 1 segment added" for i in range(5000)])])
    assert len(huge.call("search_changes", {"text": "segment"})) <= MAX_RESULT_CHARS + 80
    assert t.used == ["rm_rf", "get_commit(1)"]


# --------------------------------------------------------------------------
# OpenAI tool loop
# --------------------------------------------------------------------------

def _call(name, args, id_):
    return types.SimpleNamespace(id=id_, function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeOpenAIChat:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        msg = (types.SimpleNamespace(content=None, tool_calls=step) if isinstance(step, list)
               else types.SimpleNamespace(content=step, tool_calls=None))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def openai_llm(script):
    chat = FakeOpenAIChat(script)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=chat))
    return LLMClient(client=client, provider="openai", model="gpt-test"), chat


def test_openai_tool_loop():
    llm, chat = openai_llm([[_call("component_history", {"ref": "U6"}, "call_1")],
                            "U6 changed to MCP73831T-2ACI/OT in bbbbbbb."])
    t = tools()
    assert llm.generate_with_tools("question", t) == "U6 changed to MCP73831T-2ACI/OT in bbbbbbb."
    second = chat.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_1"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"
    assert "MCP73831T-2ACI/OT" in second[-1]["content"]
    assert "TOOLS" in second[0]["content"] and t.used == ["component_history(U6)"]


def test_openai_tool_rounds_are_capped(monkeypatch):
    monkeypatch.setattr(llm_client, "MAX_TOOL_ROUNDS", 2)
    llm, chat = openai_llm([[_call("list_commits", {}, "1")], [_call("list_commits", {}, "2")], "done"])
    assert llm.generate_with_tools("q", tools()) == "done"
    assert chat.requests[-1]["tool_choice"] == "none"


def test_busy_model_during_tools_falls_back(monkeypatch):
    monkeypatch.setattr(providers, "_sleep", lambda s: None)
    monkeypatch.setenv("FLUXDIFF_LLM_FALLBACK_MODELS", "gpt-backup")
    busy = Exception("503 UNAVAILABLE high demand")
    llm, chat = openai_llm([busy, busy, [_call("list_commits", {}, "1")], "answer"])
    t = tools()
    assert llm.generate_with_tools("q", t) == "answer"
    assert [r["model"] for r in chat.requests] == ["gpt-test", "gpt-test", "gpt-backup", "gpt-backup"]
    assert t.used == ["list_commits"]


# --------------------------------------------------------------------------
# Gemini tool loop (stand-in for google-genai types and client)
# --------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Part(_Obj):
    @staticmethod
    def from_text(text):
        return _Part(text=text)


GT = types.SimpleNamespace(
    Content=_Obj, Part=_Part, FunctionResponse=_Obj, FunctionDeclaration=_Obj, Tool=_Obj,
    ToolConfig=_Obj, FunctionCallingConfig=_Obj, AutomaticFunctionCallingConfig=_Obj,
    GenerateContentConfig=_Obj)


class FakeGeminiModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append((model, list(contents), config))
        step = self.script.pop(0)
        if isinstance(step, list):
            model_turn = _Obj(role="model", parts=["<signed function-call part>"])
            return types.SimpleNamespace(function_calls=step, text=None,
                                         candidates=[types.SimpleNamespace(content=model_turn)])
        return types.SimpleNamespace(function_calls=None, text=step, candidates=[])


def test_gemini_tool_loop_returns_signed_turns_unchanged():
    models = FakeGeminiModels([[_Obj(name="get_findings", args={"severity": "CRITICAL"}, id="fc1")],
                               "J1's shield pads moved off GND in ccccccc."])
    llm = LLMClient(client=types.SimpleNamespace(models=models), provider="gemini",
                    model="gemini-3.8-flash", gemini_types=GT)
    t = tools()
    assert llm.generate_with_tools("q", t).startswith("J1's shield pads")
    _, contents, config = models.calls[1]
    assert contents[1].parts == ["<signed function-call part>"]           # model turn as received
    response_part = contents[2].parts[0].function_response
    assert contents[2].role == "user" and response_part.id == "fc1"
    assert "J1 pad S1" in response_part.response["result"]
    assert config.tool_config.function_calling_config.mode == "AUTO"
    assert config.automatic_function_calling.disable is True
    assert [d.name for d in config.tools[0].function_declarations][:2] == ["list_commits", "get_commit"]
    assert t.used == ["get_findings(CRITICAL)"]


# --------------------------------------------------------------------------
# ChatEngine with tools
# --------------------------------------------------------------------------

class ToolLLM(RecordingLLM):
    """Calls one tool, then answers; can be made to fail."""

    def __init__(self, tool=("component_history", {"ref": "U6"}), fail=None):
        super().__init__()
        self.tool, self.tool_fail = tool, fail

    def generate_with_tools(self, prompt, tools):
        if self.tool_fail:
            raise self.tool_fail
        self.tool_result = tools.call(*self.tool)
        return "answer using tools"


def engine_with(llm, tmp_path):
    emb = fake_embedder()
    store = VectorStore(db_path=str(tmp_path / "db"))
    store.add_documents(DOCS, emb.embed_documents(DOCS), embedding=emb.signature)
    return ChatEngine(retriever=Retriever(embedder=emb, store=store), llm=llm)


def test_engine_reports_tools_and_cites_their_commits(tmp_path):
    reply = engine_with(ToolLLM(), tmp_path).ask("history of U6?")
    assert reply.answer == "answer using tools" and reply.tools_used == ["component_history(U6)"]
    assert B in {s["commit"] for s in reply.sources}


def test_engine_falls_back_to_plain_answer_when_tools_fail(tmp_path):
    llm = ToolLLM(fail=ValueError("tools not supported by this model"))
    reply = engine_with(llm, tmp_path).ask("hello")
    assert reply.answer == "answer 1" and reply.tools_used == []     # RecordingLLM's plain answer


def test_engine_does_not_retry_plain_when_models_are_busy(tmp_path):
    with pytest.raises(LLMBusy):
        engine_with(ToolLLM(fail=LLMBusy("busy")), tmp_path).ask("hello")


def test_tools_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXDIFF_CHAT_TOOLS", "0")
    reply = engine_with(ToolLLM(), tmp_path).ask("history of U6?")
    assert reply.answer == "answer 1" and reply.tools_used == []
