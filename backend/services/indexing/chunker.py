"""Domain-aware chunking (spec §13). Chunk by source kind, not a universal
fixed size. Pure functions — no I/O, fully unit-tested.
"""
from __future__ import annotations

import re

# Document kinds (also used as episodic_documents.document_type values).
DOC_MESSAGE = "message"          # one user/assistant message or logical turn
DOC_TOOL_TRACE = "tool_trace"    # goal + tool + key args + outcome
DOC_MEETING = "meeting"          # topic/speaker block
DOC_EMAIL = "email"              # subject + normalized body
DOC_FACT = "fact"                # decision/fact → one record
DOC_RUNBOOK = "runbook"          # skill/runbook → heading-aware sections
DOC_PROSE = "prose"              # 400-800 tokens, 50-100 overlap
DOC_TOOL_OUTPUT = "tool_output"  # oversized → short projection + artifact ref

_PROSE_TARGET_TOKENS = 600       # within the 400-800 band
_PROSE_OVERLAP_TOKENS = 80       # within the 50-100 band
_TOOL_OUTPUT_CAP_TOKENS = 400


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()]


def chunk_prose(text: str, *, target_tokens: int = _PROSE_TARGET_TOKENS,
                overlap_tokens: int = _PROSE_OVERLAP_TOKENS) -> list[str]:
    """Sliding window over paragraphs, ~target tokens per chunk with overlap.
    Keeps paragraph boundaries; a single oversized paragraph becomes its own
    chunk rather than being split mid-sentence."""
    paras = _split_paragraphs(text)
    if not paras:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for para in paras:
        ptok = estimate_tokens(para)
        if cur and cur_tokens + ptok > target_tokens:
            chunks.append("\n\n".join(cur))
            # carry overlap: keep trailing paragraphs up to overlap budget
            carry: list[str] = []
            carry_tokens = 0
            for prev in reversed(cur):
                t = estimate_tokens(prev)
                if carry_tokens + t > overlap_tokens:
                    break
                carry.insert(0, prev)
                carry_tokens += t
            cur = carry
            cur_tokens = carry_tokens
        cur.append(para)
        cur_tokens += ptok
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def chunk_by_headings(markdown: str) -> list[str]:
    """Split on markdown headings (``#``..``######``). Content before the
    first heading is its own chunk."""
    lines = markdown.splitlines()
    chunks: list[str] = []
    cur: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s+\S", line) and cur:
            block = "\n".join(cur).strip()
            if block:
                chunks.append(block)
            cur = [line]
        else:
            cur.append(line)
    block = "\n".join(cur).strip()
    if block:
        chunks.append(block)
    return chunks


def _truncate_tokens(text: str, cap_tokens: int) -> str:
    cap_chars = cap_tokens * 4
    if len(text) <= cap_chars:
        return text
    return text[:cap_chars].rstrip() + " …[truncated]"


def chunk_document(document_type: str, content: str, *,
                   artifact_ref: str | None = None) -> list[str]:
    """Dispatch chunking by document kind. Returns a list of chunk strings
    (often a single element for short, atomic sources)."""
    content = (content or "").strip()
    if not content:
        return []
    if document_type == DOC_PROSE:
        return chunk_prose(content)
    if document_type == DOC_RUNBOOK:
        return chunk_by_headings(content)
    if document_type == DOC_TOOL_OUTPUT:
        projection = _truncate_tokens(content, _TOOL_OUTPUT_CAP_TOKENS)
        if artifact_ref:
            projection += f"\n[full output: {artifact_ref}]"
        return [projection]
    # message / tool_trace / meeting / email / fact: atomic single chunk.
    return [content]
