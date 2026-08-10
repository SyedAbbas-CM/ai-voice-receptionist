"""Reproducibility manifest for benchmark runs.

Ported from /Users/az/Desktop/LangChain/evals/manifest.py (2026-08-10).
Every benchmark run writes results.json + results.manifest.json side by
side.  The manifest captures enough state that any run can be replayed
weeks later against the same inputs.

Why this matters here: our RAG uses per-tenant chunks + evolving
embeddings + a router LLM whose primary changes over time.  Without a
manifest we can't tell if "P95 went from 82ms → 130ms" is because the
data grew, we swapped embedders, or the router elected a slower model.
The manifest pins all three so regressions are traceable.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def sha256_file(path: Path) -> str:
    """Stream-hash a file in 1MB blocks.  Returns hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> tuple[str, bool]:
    """Return (sha, dirty).  Empty string + False if not in a git repo."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha, bool(status)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "", False


def build_manifest(
    *,
    run_id: str,
    dataset_path: Optional[Path | str] = None,
    collection_name: str = "",
    collection_manifest: Optional[dict] = None,
    provider_models: Optional[dict] = None,
    command: Optional[str | list[str]] = None,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    """Build a manifest dict for a benchmark run.

    Args:
        run_id: unique identifier, typically `<name>-<ts>`.
        dataset_path: path to the JSONL/CSV of eval inputs.  Its sha256
            gets captured — reruns against a mutated dataset are visible.
        collection_name: name of the vector collection benched (empty
            string is fine for scoring-only runs).
        collection_manifest: optional embedded manifest of the collection
            itself (chunk count, embedder version).
        provider_models: dict of role → provider:model, e.g.
            {"embedder": "BAAI/bge-small-en-v1.5", "llm": "cerebras:gpt-oss-120b"}.
        command: CLI invocation that produced this run.
        extra: arbitrary key-value pairs (thresholds, config).
    """
    sha, dirty = _git_sha()
    dataset_sha = ""
    dataset_path_str = ""
    if dataset_path:
        p = Path(dataset_path)
        dataset_path_str = str(p)
        if p.exists():
            dataset_sha = sha256_file(p)
    if isinstance(command, list):
        command = " ".join(command)
    return {
        "run_id": run_id,
        "git_sha": sha,
        "git_dirty": dirty,
        "dataset_sha": dataset_sha,
        "dataset_path": dataset_path_str,
        "collection_name": collection_name,
        "collection_manifest": collection_manifest or {},
        "provider_models": provider_models or {},
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "command": command or "",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "extra": extra or {},
    }


def write_manifest_beside(results_path: Path | str, manifest: dict) -> Path:
    """Write manifest to <results>.manifest.json next to the results file."""
    p = Path(results_path)
    manifest_path = p.with_suffix(p.suffix + ".manifest.json") if p.suffix != ".json" else p.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


_REQUIRED = {"run_id", "git_sha", "python_version", "created_at_utc"}


def validate_manifest(manifest: dict) -> list[str]:
    """Return list of missing/empty required fields (empty list = valid)."""
    missing = []
    for k in _REQUIRED:
        v = manifest.get(k)
        if not v:
            missing.append(k)
    return missing
