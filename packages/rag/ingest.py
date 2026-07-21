"""RAG ingest CLI.

Walks a vertical's sample-data folder for business.json + any *.md docs,
chunks, embeds via the configured embedder, upserts into the configured
retriever. Idempotent — rerun to reingest.

Usage:
    python -m packages.rag.ingest --vertical clinic
    python -m packages.rag.ingest --vertical restaurant --docs sample-data/restaurant/docs/
    python -m packages.rag.ingest --profile /path/to/business.json --docs /path/to/docs/

Env:
    RAG_RETRIEVER    sqlite (default) | supabase | langchain
    RAG_EMBEDDER     local (default, BGE-small-en) | openai
    RAG_DB_PATH      data/rag/kb.db (default)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from packages.rag import build_embedder, build_retriever
from packages.rag.chunker import (
    chunk_business_profile,
    chunk_markdown_file,
)
from packages.schemas import BusinessProfile


VERTICAL_TO_SLUG = {
    "clinic": "clinic",
    "restaurant": "restaurant",
    "real_estate": "real-estate",
    "wholesaler": "subtodealz",
    "subto": "subtodealz",
}


def resolve_paths(args) -> tuple[Path, list[Path]]:
    if args.profile:
        profile = Path(args.profile)
        docs_dir = Path(args.docs) if args.docs else None
    elif args.vertical:
        slug = VERTICAL_TO_SLUG.get(args.vertical.lower(), args.vertical.lower())
        profile = REPO_ROOT / "sample-data" / slug / "business.json"
        docs_dir = Path(args.docs) if args.docs else (REPO_ROOT / "sample-data" / slug / "docs")
    else:
        raise SystemExit("pass --vertical or --profile")

    if not profile.exists():
        raise SystemExit(f"business profile not found: {profile}")

    md_files: list[Path] = []
    if docs_dir and docs_dir.exists():
        md_files = sorted(docs_dir.rglob("*.md"))
    return profile, md_files


async def main_async(args) -> int:
    profile_path, md_files = resolve_paths(args)

    business = BusinessProfile(**json.loads(profile_path.read_text()))
    print(f"[ingest] business: {business.name}  (id={business.id})")
    print(f"[ingest] profile:  {profile_path}")
    print(f"[ingest] markdown: {len(md_files)} file(s)")

    embedder = build_embedder(args.embedder or "local")
    retriever = build_retriever(args.retriever or "sqlite", embedder=embedder)

    # Chunk phase
    t0 = time.time()
    chunks = chunk_business_profile(business)
    for md in md_files:
        chunks.extend(chunk_markdown_file(md, business_id=business.id))
    chunk_time = time.time() - t0
    print(f"[ingest] chunked {len(chunks)} chunks in {chunk_time:.2f}s")

    # By kind
    from collections import Counter
    by_kind = Counter(c.kind.value for c in chunks)
    for kind, n in sorted(by_kind.items()):
        print(f"           {kind:10s} {n}")

    if args.dry_run:
        print("[ingest] --dry-run set; not writing to store")
        return 0

    # Upsert phase
    t0 = time.time()
    n = await retriever.upsert(chunks)
    up_time = time.time() - t0
    print(f"[ingest] upserted {n} chunks in {up_time:.2f}s")
    print(f"[ingest] total chunks in store for {business.id}: {await retriever.size(business.id)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m packages.rag.ingest")
    p.add_argument("--vertical", help="Vertical slug: clinic | restaurant | real_estate | wholesaler")
    p.add_argument("--profile", help="Path to a business.json (overrides --vertical)")
    p.add_argument("--docs", help="Docs folder to scan for *.md")
    p.add_argument("--retriever", default=None, help="Retriever backend (default: sqlite)")
    p.add_argument("--embedder", default=None, help="Embedder backend (default: local)")
    p.add_argument("--dry-run", action="store_true", help="Chunk + report, don't write")
    args = p.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
