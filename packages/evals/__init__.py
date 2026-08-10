"""Reproducible benchmark harness.

Ported 2026-08-10 from /Users/az/Desktop/LangChain/evals/manifest.py.
Every benchmark run writes results.json + results.manifest.json side by
side.  The manifest captures git SHA + dataset SHA + provider models +
Python version + command so any run can be replayed weeks later against
the same inputs to compare.

Usage:
    from packages.evals import build_manifest, write_manifest_beside

    manifest = build_manifest(
        run_id=f"rag-latency-{ts}",
        dataset_path="tests/rag/smile_dental_queries.jsonl",
        collection_name="smile-dental-001",
        provider_models={
            "embedder": "BAAI/bge-small-en-v1.5",
            "llm": "cerebras:gpt-oss-120b",
        },
    )
    write_manifest_beside(results_path, manifest)
"""
from .manifest import (
    build_manifest,
    write_manifest_beside,
    sha256_file,
    validate_manifest,
)

__all__ = [
    "build_manifest",
    "write_manifest_beside",
    "sha256_file",
    "validate_manifest",
]
