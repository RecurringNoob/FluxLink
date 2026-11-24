"""OpenAI / Gemini provider selection, Gemini backends, and embedding-model safety."""
import math
import types

import pytest

import test_rag                                    # installs the faiss stand-in
from test_rag import RecordingLLM, board_repo, doc, fake_embedder  # noqa: F401  (fixture)
from fluxdiff.rag import providers
from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.embedding.embedder import Embedder
from fluxdiff.rag.embedding.vector_store import EmbeddingMismatch, VectorStore
from fluxdiff.rag.ingest.run_ingest import run_ingestion
from fluxdiff.rag.llm.llm_client import LLMClient, LLMError
from fluxdiff.rag.retrieval.retriever import Retriever
from fluxdiff.rag.schemas import RAGQuery

KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "FLUXDIFF_LLM_PROVIDER",
        "FLUXDIFF_EMBEDDING_PROVIDER", "FLUXDIFF_LLM_MODEL", "FLUXDIFF_EMBEDDING_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FLUXLINK_ERP_DEMO", raising=False)


# --------------------------------------------------------------------------
# A stand-in for the google-genai SDK (client.models.* and genai.types)
# --------------------------------------------------------------------------

class _Part:
    def __init__(self, text):
        self.text = text

    @staticmethod
    def from_text(text):
        return _Part(text)


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


GEMINI_TYPES = types.SimpleNamespace(Content=_Content, Part=_Part,
                                     EmbedContentConfig=_Config, GenerateContentConfig=_Config)


def _gvec(text, dim=12):
    v = [0.0] * dim
    for i, ch in enumerate(text[:96]):
        v[i % dim] += ord(ch) / 1000.0
    return v


class FakeGeminiModels:
    def __init__(self, reply="gemini answer", fail=None):
        self.embed_calls, self.generate_calls = [], []
        self.reply, self.fail = reply, fail

    def embed_content(self, model, contents, config=None):
        self.embed_calls.append((model, contents, config))
        if isinstance(contents, str):
            texts = [contents]
        elif contents and isinstance(contents[0], _Content):
            texts = [c.parts[0].text for c in contents]
        elif model == "gemini-embedding-2":
            texts = [" ".join(contents)]       # real API: a plain list -> ONE aggregated vector
        else:
            texts = list(contents)
        return types.SimpleNamespace(embeddings=[types.SimpleNamespace(values=_gvec(t)) for t in texts])

    def generate_content(self, model, contents, config=None):
        self.generate_calls.append((model, contents, config))
        if self.fail:
            raise self.fail
        cand = types.SimpleNamespace(finish_reason="SAFETY")
        return types.SimpleNamespace(text=self.reply, candidates=[cand])


def gemini_client(**kw):
    return types.SimpleNamespace(models=FakeGeminiModels(**kw))


def gemini_embedder(model="gemini-embedding-2", client=None):
    return Embedder(client=client or gemini_client(), provider="gemini", model=model,
                    gemini_types=GEMINI_TYPES)


# --------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({}, "openai"),
    ({"OPENAI_API_KEY": "x"}, "openai"),
    ({"GEMINI_API_KEY": "x"}, "gemini"),
    ({"GOOGLE_API_KEY": "x"}, "gemini"),
    ({"OPENAI_API_KEY": "x", "GEMINI_API_KEY": "y"}, "openai"),
    ({"OPENAI_API_KEY": "x", "FLUXDIFF_LLM_PROVIDER": "Gemini"}, "gemini"),
])
def test_llm_provider_selection(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert providers.llm_provider() == expected


def test_models_default_per_provider_and_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert providers.describe() == {"llm": "gemini:gemini-3.8-flash",
                                    "embedding": "gemini:gemini-embedding-2"}
    monkeypatch.setenv("FLUXDIFF_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("FLUXDIFF_LLM_MODEL", "gemini-3.5-flash-lite")
    assert providers.describe() == {"llm": "gemini:gemini-3.5-flash-lite",
                                    "embedding": "openai:text-embedding-3-small"}


def test_unknown_provider_and_missing_keys_are_explained(monkeypatch):
    monkeypatch.setenv("FLUXDIFF_LLM_PROVIDER", "claude")
    with pytest.raises(providers.ProviderError, match="not supported"):
        providers.llm_provider()
    with pytest.raises(providers.ProviderError, match="GEMINI_API_KEY"):
        providers.require_key("gemini")
    with pytest.raises(providers.ProviderError, match="OPENAI_BASE_URL"):
        providers.require_key("openai")


# --------------------------------------------------------------------------
# Gemini embeddings
# --------------------------------------------------------------------------

def test_gemini_embedding_2_one_vector_per_text_with_task_prefixes():
    client = gemini_client()
    emb = gemini_embedder(client=client)
    vectors = emb.embed_documents([doc("alpha"), doc("beta"), doc("gamma")])
    assert len(vectors) == 3 and vectors[0] != vectors[1]
    _, contents, config = client.models.embed_calls[0]
    assert [c.parts[0].text for c in contents] == [
        "title: none | text: alpha", "title: none | text: beta", "title: none | text: gamma"]
    assert config is None                       # gemini-embedding-2 has no task_type

    emb.embed_query("where is R5?")
    _, contents, _ = client.models.embed_calls[-1]
    assert contents[0].parts[0].text == "task: search result | query: where is R5?"
    assert emb.signature == "gemini:gemini-embedding-2" and emb.max_input_chars == 16_000


def test_gemini_embedding_001_uses_task_type_and_normalises():
    client = gemini_client()
    emb = gemini_embedder("gemini-embedding-001", client)
    [v] = emb.embed_documents([doc("alpha")])
    _, contents, config = client.models.embed_calls[0]
    assert contents == ["alpha"] and config.task_type == "RETRIEVAL_DOCUMENT"
    assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-9)
    emb.embed_query("q")
    assert client.models.embed_calls[-1][2].task_type == "RETRIEVAL_QUERY"
    assert emb.max_input_chars == 5_000         # 2,048-token input limit


def test_gemini_requests_are_batched():
    client = gemini_client()
    gemini_embedder(client=client).embed_documents([doc(f"d{i}") for i in range(250)])
    assert [len(c[1]) for c in client.models.embed_calls] == [100, 100, 50]


# --------------------------------------------------------------------------
# Gemini chat
# --------------------------------------------------------------------------

def test_gemini_llm_sends_system_instruction():
    client = gemini_client(reply="  The shield pads moved.  ")
    llm = LLMClient(client=client, provider="gemini", model="gemini-3.8-flash",
                    gemini_types=GEMINI_TYPES)
    assert llm.generate_response("question") == "The shield pads moved."
    model, contents, config = client.models.generate_calls[0]
    assert model == "gemini-3.8-flash" and contents == "question"
    assert "FluxDiff" in config.system_instruction
    assert not hasattr(config, "temperature")


def test_gemini_llm_errors_become_llm_errors(sleeps):
    empty = LLMClient(client=gemini_client(reply=None), provider="gemini", gemini_types=GEMINI_TYPES)
    with pytest.raises(LLMError, match="SAFETY"):
        empty.generate_response("q")
    failing = LLMClient(client=gemini_client(fail=ConnectionError("quota")), provider="gemini",
                        gemini_types=GEMINI_TYPES)
    with pytest.raises(LLMError, match="quota"):
        failing.generate_response("q")


def test_chat_end_to_end_on_gemini(tmp_path):
    emb = gemini_embedder()
    store = VectorStore(db_path=str(tmp_path / "g"))
    docs = [doc("Commit: c\nType: Net\nChanges: R5 added", commit="c" * 40, type="net")]
    store.add_documents(docs, emb.embed_documents(docs), embedding=emb.signature)
    client = gemini_client(reply="R5 ties the shield to GND.")
    llm = LLMClient(client=client, provider="gemini", gemini_types=GEMINI_TYPES)
    reply = ChatEngine(retriever=Retriever(embedder=emb, store=store), llm=llm).ask("why R5?")
    assert reply.answer == "R5 ties the shield to GND." and reply.sources[0]["type"] == "net"
    assert "R5 added" in client.models.generate_calls[0][1]


# --------------------------------------------------------------------------
# Indexes remember their embedding model
# --------------------------------------------------------------------------

def test_index_records_its_embedding_model(tmp_path):
    emb = fake_embedder()
    store = VectorStore(db_path=str(tmp_path / "db"))
    store.add_documents([doc("x")], emb.embed_documents([doc("x")]), embedding=emb.signature)
    assert VectorStore(db_path=str(tmp_path / "db")).embedding == "openai:text-embedding-3-small"


def test_querying_with_another_embedding_model_is_refused(tmp_path):
    openai_emb = fake_embedder()
    store = VectorStore(db_path=str(tmp_path / "db"))
    store.add_documents([doc("x")], openai_emb.embed_documents([doc("x")]),
                        embedding=openai_emb.signature)
    gem = gemini_embedder()
    with pytest.raises(EmbeddingMismatch, match="Re-index"):
        Retriever(embedder=gem, store=store).retrieve_with_query(RAGQuery(query="x"))
    assert gem.client.models.embed_calls == []          # refused before any API call


def test_legacy_index_with_other_dimension_is_refused_not_crashed(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "db"))
    store.add_documents([doc("x")], fake_embedder().embed_documents([doc("x")]))  # no signature
    with pytest.raises(EmbeddingMismatch, match="dimensional"):
        store.similarity_search(_gvec("x"))                                     # 12-d vs 8-d


def test_reindexing_with_a_new_model_rebuilds(board_repo, tmp_path):
    store = VectorStore(db_path=str(tmp_path / "db"))
    run_ingestion(repo_path=str(board_repo), embedder=fake_embedder(), store=store)
    before = len(store)
    logs = []
    run_ingestion(repo_path=str(board_repo), embedder=gemini_embedder(), store=store,
                  log=logs.append)
    assert any("Embedding model changed" in m for m in logs)
    assert store.embedding == "gemini:gemini-embedding-2" and len(store) == before
    assert store.index.d == 12


def test_chunk_size_follows_the_embedding_model(board_repo, tmp_path):
    store = VectorStore(db_path=str(tmp_path / "db"))
    run_ingestion(repo_path=str(board_repo), embedder=gemini_embedder("gemini-embedding-001"),
                  store=store)
    assert max(len(d.content) for d in store.documents) <= 5_000


# --------------------------------------------------------------------------
# Busy models: retry, then fall back
# --------------------------------------------------------------------------

# The error Gemini returned in the hosted demo (Nov 2026).
GEMINI_503 = ("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently "
              "experiencing high demand. Spikes in demand are usually temporary. Please try again "
              "later.', 'status': 'UNAVAILABLE'}}")


class ServerError(Exception):
    pass


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(providers, "_sleep", waited.append)
    return waited


class FlakyModels(FakeGeminiModels):
    """generate_content fails per model according to a script of outcomes."""

    def __init__(self, script):
        super().__init__()
        self.script = {m: list(outcomes) for m, outcomes in script.items()}

    def generate_content(self, model, contents, config=None):
        self.generate_calls.append((model, contents, config))
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return types.SimpleNamespace(text=outcome, candidates=[])


def flaky_llm(script):
    client = types.SimpleNamespace(models=FlakyModels(script))
    return LLMClient(client=client, provider="gemini", model="gemini-3.8-flash",
                     gemini_types=GEMINI_TYPES), client.models


def test_busy_model_falls_back(sleeps):
    llm, models = flaky_llm({"gemini-3.8-flash": [ServerError(GEMINI_503)] * 2,
                             "gemini-3.5-flash-lite": ["answer from lite"]})
    assert llm.generate_response("q") == "answer from lite"
    assert llm.last_model == "gemini-3.5-flash-lite"
    assert [c[0] for c in models.generate_calls] == ["gemini-3.8-flash"] * 2 + ["gemini-3.5-flash-lite"]
    assert sleeps == [1.5]


def test_brief_spike_is_retried_on_the_same_model(sleeps):
    llm, models = flaky_llm({"gemini-3.8-flash": [ServerError(GEMINI_503), "second try"]})
    assert llm.generate_response("q") == "second try" and llm.last_model == "gemini-3.8-flash"


def test_all_models_busy_gives_a_readable_error(sleeps):
    llm, _ = flaky_llm({"gemini-3.8-flash": [ServerError(GEMINI_503)] * 2,
                        "gemini-3.5-flash-lite": [ServerError("429 RESOURCE_EXHAUSTED quota")] * 2})
    with pytest.raises(LLMError) as err:
        llm.generate_response("q")
    message = str(err.value)
    assert message.startswith("The model is busy right now")
    assert "503 UNAVAILABLE: This model is currently experiencing high demand" in message
    assert "{'error'" not in message and "try again" in message


def test_permanent_errors_are_not_retried(sleeps):
    llm, models = flaky_llm({"gemini-3.8-flash": [ValueError("400 INVALID_ARGUMENT: bad model")]})
    with pytest.raises(LLMError, match="INVALID_ARGUMENT"):
        llm.generate_response("q")
    assert len(models.generate_calls) == 1 and sleeps == []


def test_fallbacks_are_configurable(monkeypatch):
    monkeypatch.setenv("FLUXDIFF_LLM_FALLBACK_MODELS", "a, b ,gemini-3.8-flash")
    assert providers.llm_fallback_models("gemini", "gemini-3.8-flash") == ["a", "b"]
    monkeypatch.setenv("FLUXDIFF_LLM_FALLBACK_MODELS", "")
    assert providers.llm_fallback_models("gemini", "gemini-3.8-flash") == []
    monkeypatch.delenv("FLUXDIFF_LLM_FALLBACK_MODELS")
    assert providers.llm_fallback_models("openai", "gpt-4o-mini") == []


def test_query_embedding_is_retried_then_reported(sleeps):
    from fluxdiff.rag.embedding.embedder import EmbeddingError
    calls = []

    class Busy(FakeGeminiModels):
        def embed_content(self, model, contents, config=None):
            calls.append(model)
            if len(calls) == 1:
                raise ServerError(GEMINI_503)
            return super().embed_content(model, contents, config)

    emb = gemini_embedder(client=types.SimpleNamespace(models=Busy()))
    assert len(emb.embed_query("q")) == 12 and sleeps == [1.5]

    class Down(FakeGeminiModels):
        def embed_content(self, model, contents, config=None):
            raise ServerError(GEMINI_503)

    down = gemini_embedder(client=types.SimpleNamespace(models=Down()))
    with pytest.raises(EmbeddingError, match="high demand"):
        down.embed_query("q")


@pytest.mark.parametrize("error,transient", [
    (ServerError(GEMINI_503), True),
    (Exception("429 RESOURCE_EXHAUSTED"), True),
    (type("RateLimitError", (Exception,), {})("slow down"), True),
    (type("E", (Exception,), {"status_code": 503})("x"), True),
    (ValueError("400 INVALID_ARGUMENT"), False),
    (PermissionError("401 API key not valid"), False),
])
def test_transient_detection(error, transient):
    assert providers.is_transient(error) is transient
