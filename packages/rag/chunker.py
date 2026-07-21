"""Chunker: turn business profiles + markdown docs into Chunks.

Voice-agent-specific rules:
  - Each chunk must be independently speakable (no "see above")
  - Each chunk gets a source citation so the LLM can escalate on
    "where did you learn that?"
  - FAQ Q+A pairs stay together as one chunk (never split)
  - Markdown gets split at heading boundaries, not by token count
    (heading-scoped chunks preserve context)
  - Tables and code blocks are dropped entirely — not speakable
"""
from __future__ import annotations

import re
from pathlib import Path

from packages.schemas import BusinessProfile

from .types import Chunk, ChunkKind


MAX_CHUNK_CHARS = 800     # ~150 words per chunk — enough context, still bounded


def chunk_business_profile(business: BusinessProfile) -> list[Chunk]:
    """Extract FAQs, services, hours, and address as chunks.

    Same chunker used for every vertical — the shape of business.json
    is stable across clinic/restaurant/real-estate/wholesaler.
    """
    chunks: list[Chunk] = []
    bid = business.id

    # FAQs — one chunk per Q/A pair. Highest-precision KB source.
    for question, answer in (business.faqs or {}).items():
        text = f"Q: {question}\nA: {answer}"
        chunks.append(Chunk(
            text=text,
            business_id=bid,
            source=f"business.json:faqs.{_slug(question)}",
            kind=ChunkKind.FAQ,
            metadata={"question": question},
        ))

    # Services — one chunk per service, including duration + price + description
    for svc in (business.services or []):
        parts = [f"Service: {svc.name}"]
        parts.append(f"Duration: {svc.duration_minutes} minutes")
        if svc.description:
            parts.append(f"Description: {svc.description}")
        if svc.price:
            parts.append(f"Price: {svc.price}")
        chunks.append(Chunk(
            text="\n".join(parts),
            business_id=bid,
            source=f"business.json:services.{_slug(svc.name)}",
            kind=ChunkKind.SERVICE,
            metadata={"service_name": svc.name, "duration_minutes": svc.duration_minutes},
        ))

    # Hours — one summary chunk (all days) so a single retrieval answers
    # "what are your hours" and "are you open Sunday" alike.
    hours_lines = []
    days = ["monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"]
    for day in days:
        hours = getattr(business.hours, day, None) if business.hours else None
        hours_lines.append(f"{day.capitalize()}: {hours or 'closed'}")
    chunks.append(Chunk(
        text="Business hours\n" + "\n".join(hours_lines),
        business_id=bid,
        source="business.json:hours",
        kind=ChunkKind.HOURS,
    ))

    # Address, escalation phone, persona — as one "about" chunk
    about_lines = [f"Business: {business.name}"]
    if business.address:
        about_lines.append(f"Address: {business.address}")
    if business.escalation_phone:
        about_lines.append(f"Callback line: {business.escalation_phone}")
    if business.voice_persona:
        about_lines.append(f"Tone: {business.voice_persona}")
    chunks.append(Chunk(
        text="\n".join(about_lines),
        business_id=bid,
        source="business.json:about",
        kind=ChunkKind.POLICY,
    ))

    return chunks


# Splits a markdown document at H1/H2/H3 boundaries.
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def chunk_markdown(text: str, source: str, business_id: str) -> list[Chunk]:
    """Split a markdown doc into heading-scoped chunks.

    Each chunk gets its heading path (e.g. "Policies > Cancellation") in
    metadata so the retrieved answer can be introduced as
    "Under our cancellation policy, ..."
    """
    if not text:
        return []

    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    current_body: list[str] = []
    current_source_suffix = "top"

    def flush():
        body = "\n".join(current_body).strip()
        if not body:
            return
        # Strip tables + code fences before storing — unspeakable.
        cleaned = _strip_unspeakable_blocks(body)
        # Split further if too long
        for piece in _soft_split(cleaned, MAX_CHUNK_CHARS):
            heading_path = " > ".join(h for _, h in heading_stack) or "top"
            chunks.append(Chunk(
                text=piece,
                business_id=business_id,
                source=f"{source}:{current_source_suffix}",
                kind=ChunkKind.DOC,
                metadata={"heading_path": heading_path},
            ))

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            current_body = []
            level = len(m.group(1))
            title = m.group(2).strip()
            # Pop deeper-or-equal levels off the stack
            heading_stack = [(lvl, ttl) for lvl, ttl in heading_stack if lvl < level]
            heading_stack.append((level, title))
            current_source_suffix = _slug(title)
        else:
            current_body.append(line)
    flush()
    return chunks


def chunk_markdown_file(path: str | Path, business_id: str) -> list[Chunk]:
    p = Path(path)
    return chunk_markdown(p.read_text(encoding="utf-8"), source=p.name, business_id=business_id)


# ---- helpers ----

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "unnamed"


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_TABLE_LINE_RE = re.compile(r"^\|.+\|\s*$", re.MULTILINE)


def _strip_unspeakable_blocks(text: str) -> str:
    """Remove code fences and markdown tables entirely — they can't be
    spoken. Keep everything else."""
    text = _CODE_FENCE_RE.sub("", text)
    text = _TABLE_LINE_RE.sub("", text)
    return text.strip()


def _soft_split(text: str, max_chars: int) -> list[str]:
    """If text > max_chars, split at paragraph breaks. Never splits
    mid-sentence."""
    if len(text) <= max_chars:
        return [text]
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    out: list[str] = []
    current: list[str] = []
    running = 0
    for p in paras:
        if running + len(p) > max_chars and current:
            out.append("\n\n".join(current))
            current = [p]
            running = len(p)
        else:
            current.append(p)
            running += len(p) + 2
    if current:
        out.append("\n\n".join(current))
    return out
