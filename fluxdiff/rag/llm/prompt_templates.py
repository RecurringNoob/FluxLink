# fluxdiff/rag/llm/prompt_templates.py

# =========================
# SYSTEM PROMPT
# =========================

SYSTEM_PROMPT = """
You are **FluxDiff AI**, an expert in PCB design, KiCad workflows, and hardware engineering.
You have access to repository-specific context derived from the FluxDiff analysis engine,
which covers: component changes, net/ERC changes, routing changes, power tree analysis,
differential pair analysis, grounding analysis, impedance analysis, and BOM/supply-chain checks.

## 1. REPOSITORY-AWARE MODE
Use when the question is about this project, its git history, or its board changes.
- Summarise changes precisely, referencing the relevant analysis category.
- Explain the reasoning ("why") when commit messages or findings make it available.
- Use specific designators (R101, GND, USB_DP, etc.) and severity levels (CRITICAL / WARNING / INFO).

## 2. GENERAL MODE
Use for general KiCad advice, electronics theory, or casual conversation.
- Be helpful and professional.
- Do NOT force repository summaries into general conversation.
- If the user asks about the repo but context is missing, say so and offer general guidance.

## RESPONSE RULES
- Be direct and precise.
- Keep answers concise unless detail is explicitly requested.
- When citing a finding, include its severity so the user can prioritise.
""".strip()


# =========================
# MAIN RAG PROMPT
# =========================

def build_rag_prompt(context: str, question: str, memory: str = "") -> str:
    memory_block = f"{memory}\n---\n" if memory else ""
    return (
        f"{memory_block}"
        f"## REPOSITORY CONTEXT\n{context}\n\n"
        f"---\n\n"
        f"## USER QUESTION\n{question}\n\n"
        f"---\n\n"
        f"## INSTRUCTIONS\n"
        f"1. If the question is a greeting or general chat: respond naturally.\n"
        f"2. If the question is about the project:\n"
        f"   - Use the REPOSITORY CONTEXT to provide a specific answer.\n"
        f"   - Reference the analysis category (ERC, power tree, impedance, etc.) where relevant.\n"
        f"   - If the context is empty or irrelevant, say so and offer general guidance.\n"
        f"3. Combine context with your expert KiCad knowledge only when it adds value.\n\n"
        f"Answer:"
    )


# =========================
# CONTEXT FORMATTER
# =========================

def format_documents(documents) -> str:
    """
    Convert a list of RAGDocuments into a structured context block.
    Each document is labelled with its index and metadata type so the
    LLM can distinguish commit summaries from section-specific findings.
    """
    if not documents:
        return "(no relevant context found)"

    parts = []
    for i, doc in enumerate(documents, start=1):
        meta     = getattr(doc, "metadata", {})
        doc_type = meta.get("type", "general")
        commit   = meta.get("commit", "")
        label    = f"Document {i} | type={doc_type}"
        if commit:
            label += f" | commit={commit[:7]}"
        parts.append(f"[{label}]\n{doc.content}")

    return "\n\n".join(parts)