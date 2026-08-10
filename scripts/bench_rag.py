#!/usr/bin/env python3
"""RAG latency + retrieval-quality benchmark.

Ported design 2026-08-10 from /Users/az/Desktop/LangChain/evals/*.
Reads a JSONL of eval queries, runs each through the configured
retriever, records per-query latency + whether the expected chunk
was in the top-K, and writes results.json + results.manifest.json.

Query file format (one JSON per line):
    {"query": "Do you take Blue Cross?", "expected_chunk_ids": ["insurance-bcbs"], "tenant_id": "default", "business_id": "cedar-ridge-dental-001"}

Usage:
    python scripts/bench_rag.py --queries tests/rag/smile_dental_queries.jsonl --out data/bench/rag-$(date +%Y%m%d-%H%M%S).json
    python scripts/bench_rag.py --queries tests/rag/smile_dental_queries.jsonl --out ... --top-k 5

Metrics reported:
    - latency_p50_ms / p95_ms / p99_ms / max_ms
    - recall_at_k (fraction of queries where >=1 expected chunk lands in top-K)
    - top1_hit_rate (fraction where top-1 is an expected chunk)
    - per-query timing + hit table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "api"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True, help="JSONL of eval queries")
    ap.add_argument("--out", required=True, help="results JSON path")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=3, help="warmup queries before timing")
    args = ap.parse_args()

    queries_path = Path(args.queries)
    if not queries_path.exists():
        print(f"queries file not found: {queries_path}", file=sys.stderr)
        return 2
    rows = [json.loads(l) for l in queries_path.read_text().splitlines() if l.strip()]
    if not rows:
        print(f"no queries in {queries_path}", file=sys.stderr)
        return 2

    from packages.rag import build_retriever
    from packages.evals import build_manifest, write_manifest_beside

    retriever = build_retriever(kind="sqlite")

    # Warmup — first-hit includes model load / DB open / TLS handshake.
    warm = rows[:args.warmup]
    for r in warm:
        await retriever.search(r["query"], r["business_id"], top_k=args.top_k)

    # Timed runs
    per_query = []
    for row in rows:
        expected = set(row.get("expected_chunk_ids") or [])
        t0 = time.perf_counter()
        hits = await retriever.search(row["query"], row["business_id"], top_k=args.top_k)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        hit_ids = [h.chunk.id for h in hits]
        top1_ok = bool(expected) and hit_ids and hit_ids[0] in expected
        recall_ok = bool(expected) and any(cid in expected for cid in hit_ids)
        per_query.append({
            "query": row["query"],
            "business_id": row["business_id"],
            "expected": sorted(expected),
            "returned": hit_ids,
            "latency_ms": round(elapsed_ms, 2),
            "top1_hit": top1_ok,
            "recall_at_k": recall_ok,
        })

    latencies = [q["latency_ms"] for q in per_query]
    with_expected = [q for q in per_query if q["expected"]]

    aggregate = {
        "n_queries": len(per_query),
        "n_scored": len(with_expected),
        "top_k": args.top_k,
        "latency_p50_ms": round(statistics.median(latencies), 2),
        "latency_p95_ms": round(statistics.quantiles(latencies, n=20)[18], 2)
            if len(latencies) >= 20 else round(max(latencies), 2),
        "latency_p99_ms": round(statistics.quantiles(latencies, n=100)[98], 2)
            if len(latencies) >= 100 else round(max(latencies), 2),
        "latency_max_ms": round(max(latencies), 2),
        "latency_mean_ms": round(statistics.mean(latencies), 2),
        "recall_at_k": round(
            sum(1 for q in with_expected if q["recall_at_k"]) / max(len(with_expected), 1), 3,
        ),
        "top1_hit_rate": round(
            sum(1 for q in with_expected if q["top1_hit"]) / max(len(with_expected), 1), 3,
        ),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "config": {
            "queries_path": str(queries_path),
            "top_k": args.top_k,
            "warmup": args.warmup,
        },
        "aggregate": aggregate,
        "results": per_query,
    }, indent=2))

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    manifest = build_manifest(
        run_id=f"rag-bench-{ts}",
        dataset_path=queries_path,
        collection_name="mixed",
        provider_models={
            "retriever": type(retriever).__name__,
        },
        command=sys.argv,
        extra={
            "top_k": args.top_k,
            **aggregate,
        },
    )
    manifest_path = write_manifest_beside(out_path, manifest)

    print(f"wrote {out_path}")
    print(f"wrote {manifest_path}")
    print("aggregate:")
    for k, v in aggregate.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
