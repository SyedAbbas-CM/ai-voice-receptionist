"""Chunker tests. Verify business.json + markdown produce speakable
chunks with clean source citations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.rag import Chunk, ChunkKind
from packages.rag.chunker import chunk_business_profile, chunk_markdown
from packages.schemas import BusinessProfile


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def clinic_business() -> BusinessProfile:
    data = json.loads((REPO_ROOT / "sample-data" / "clinic" / "business.json").read_text())
    return BusinessProfile(**data)


def test_chunk_business_profile_faqs(clinic_business):
    chunks = chunk_business_profile(clinic_business)
    # Every FAQ becomes its own chunk with kind=FAQ
    faq_chunks = [c for c in chunks if c.kind == ChunkKind.FAQ]
    assert len(faq_chunks) == len(clinic_business.faqs)
    for c in faq_chunks:
        assert c.text.startswith("Q:")
        assert "A:" in c.text
        assert c.source.startswith("business.json:faqs.")


def test_chunk_business_profile_services(clinic_business):
    chunks = chunk_business_profile(clinic_business)
    svc_chunks = [c for c in chunks if c.kind == ChunkKind.SERVICE]
    assert len(svc_chunks) == len(clinic_business.services)
    for c in svc_chunks:
        assert "Service:" in c.text
        assert "Duration:" in c.text


def test_chunk_business_profile_hours(clinic_business):
    chunks = chunk_business_profile(clinic_business)
    hours = [c for c in chunks if c.kind == ChunkKind.HOURS]
    assert len(hours) == 1
    # All 7 days should be enumerated so a single retrieval hits any of them
    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        assert day in hours[0].text


def test_chunk_ids_are_content_derived(clinic_business):
    """Two runs of the chunker on the same input produce byte-identical IDs.
    This is what makes upserts idempotent — reingesting doesn't duplicate."""
    chunks_a = chunk_business_profile(clinic_business)
    chunks_b = chunk_business_profile(clinic_business)
    ids_a = sorted(c.id for c in chunks_a)
    ids_b = sorted(c.id for c in chunks_b)
    assert ids_a == ids_b


def test_chunk_markdown_splits_at_headings():
    text = """# Policies

Some intro text about our policies in general.

## Cancellation

Cancel 24 hours before your appointment. No fee if you do.

## Payment

We accept cash and cards.
"""
    chunks = chunk_markdown(text, source="policies.md", business_id="test")
    assert len(chunks) == 3   # Policies (top), Cancellation, Payment
    # Every chunk has a heading path in metadata
    for c in chunks:
        assert "heading_path" in c.metadata


def test_chunk_markdown_strips_code_fences_and_tables():
    text = """# Menu

Our menu changes weekly.

```python
def not_speakable(): pass
```

| Col | Val |
|-----|-----|
| A   | 1   |

That table is not readable over the phone.
"""
    chunks = chunk_markdown(text, source="menu.md", business_id="test")
    body = "\n".join(c.text for c in chunks)
    assert "def not_speakable" not in body
    assert "| Col |" not in body
    assert "weekly" in body
    assert "phone" in body


def test_chunk_markdown_empty_input_returns_empty():
    assert chunk_markdown("", "x.md", "b") == []
    assert chunk_markdown("   ", "x.md", "b") == []


def test_chunk_soft_split_on_long_body():
    """A single heading with a very long body should split at paragraph breaks."""
    from packages.rag.chunker import MAX_CHUNK_CHARS

    para = "This is one paragraph. " * 30    # ~700 chars
    doc = "# Header\n\n" + "\n\n".join([para] * 5)
    chunks = chunk_markdown(doc, source="long.md", business_id="test")
    # Should split into multiple chunks; each below the max
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= MAX_CHUNK_CHARS
