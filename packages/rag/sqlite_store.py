"""SQLite + sqlite-vec + FTS5 hybrid store.

Zero-config, zero-external-dep local vector store. Ships with the repo.

Schema (all in one file at CACHE_DIR/kb.db):
    chunks              — chunk_id, business_id, source, kind, text, metadata
    chunks_vec          — vec0 virtual table, 384-dim by default (BGE-small)
    chunks_fts          — FTS5 virtual table on text

Hybrid search combines cosine similarity (from vec0) and BM25 (from FTS5)
via Reciprocal Rank Fusion — same technique as Weaviate/Elasticsearch's
hybrid endpoint. Weights tunable via the `alpha` param (default 0.6
vector, 0.4 BM25). BM25 wins for exact keyword matches like insurance
brand names; vector wins for paraphrase.

Confidence: we normalize the fused score. Top-1 with score > threshold
is safe to speak; below, we escalate.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
from pathlib import Path
from typing import Optional

from app.core.config import settings

from .embedder import Embedder, LocalBGEEmbedder
from .retriever import Retriever
from .types import Chunk, ChunkKind, RetrievalHit


log = logging.getLogger(__name__)


DEFAULT_DB_PATH = "data/rag/kb.db"


class SqliteVecRetriever(Retriever):
    """Local hybrid RAG store. All-in-one SQLite file."""

    name = "sqlite"

    def __init__(
        self,
        db_path: Optional[str] = None,
        embedder: Optional[Embedder] = None,
        alpha: float = 0.6,             # weight for vector vs BM25 (0=BM25 only, 1=vector only)
    ):
        self.db_path = db_path or getattr(settings, "rag_db_path", None) or DEFAULT_DB_PATH
        self.embedder = embedder or LocalBGEEmbedder()
        self.alpha = alpha
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # sqlite-vec must be loaded per-connection
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, sqlite3.OperationalError) as e:
            raise RuntimeError(
                "sqlite-vec not loaded — install with `pip install sqlite-vec` "
                "and ensure SQLite supports loadable extensions."
            ) from e
        return conn

    def _init_schema(self, conn: sqlite3.Connection):
        if self._initialized:
            return
        dim = self.embedder.dim
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{{}}'
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_business ON chunks(business_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                embedding float[{dim}]
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, business_id UNINDEXED, chunk_id UNINDEXED,
                tokenize = 'porter'
            );
        """)
        conn.commit()
        self._initialized = True

    async def upsert(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        conn = self._connect()
        self._init_schema(conn)

        # Batch embed
        texts = [c.text for c in chunks]
        vectors = await self.embedder.embed(texts)

        written = 0
        for chunk, vec in zip(chunks, vectors):
            # Delete existing chunk-id from all three tables
            conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (chunk.id,))
            conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (self._hash_id(chunk.id),))
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk.id,))
            # Insert canonical row
            conn.execute(
                "INSERT INTO chunks (chunk_id, business_id, source, kind, text, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk.id, chunk.business_id, chunk.source, chunk.kind.value,
                 chunk.text, json.dumps(chunk.metadata)),
            )
            # Vector
            conn.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (self._hash_id(chunk.id), self._pack_vector(vec)),
            )
            # FTS
            conn.execute(
                "INSERT INTO chunks_fts (chunk_id, business_id, text) VALUES (?, ?, ?)",
                (chunk.id, chunk.business_id, chunk.text),
            )
            written += 1
        conn.commit()
        conn.close()
        return written

    async def search(
        self,
        query: str,
        business_id: str,
        top_k: int = 3,
    ) -> list[RetrievalHit]:
        if not query.strip():
            return []
        conn = self._connect()
        self._init_schema(conn)

        # Vector side: BGE prefers a query prefix per its model card
        query_prefix = "Represent this sentence for searching relevant passages: "
        vecs = await self.embedder.embed([query_prefix + query])
        query_vec = self._pack_vector(vecs[0])

        # Step 1: vector kNN.  sqlite-vec's MATCH doesn't support WHERE
        # joins on the parent table, so we can't push the tenant filter
        # into the vector query directly.  Instead, over-fetch and keep
        # pulling batches until we've collected top_k * 4 tenant-scoped
        # results OR exhausted candidates.
        #
        # Audit-3 fix (2026-08-04): the previous version fetched exactly
        # top_k * 4 rows globally then post-filtered.  Under multi-tenant
        # load, another tenant's popular chunks could occupy the entire
        # kNN window, starving the requested business of any hits.
        target = top_k * 4
        max_scan = max(target * 16, 128)  # cap so a pathological query doesn't scan the whole table
        knn = conn.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?",
            (query_vec, max_scan),
        ).fetchall()

        # Step 2: build a rowid->chunk_id map for THIS batch of rowids only.
        rowid_set = {r["rowid"] for r in knn}
        rowid_to_chunk_id = self._build_rowid_map(conn, rowid_set)

        # Step 3: fetch chunk rows, filter by business_id, stop at target
        vector_hits: dict[str, tuple[Chunk, float]] = {}
        for row in knn:
            if len(vector_hits) >= target:
                break
            cid = rowid_to_chunk_id.get(row["rowid"])
            if not cid:
                continue
            chunk_row = conn.execute(
                "SELECT chunk_id, business_id, source, kind, text, metadata "
                "FROM chunks WHERE chunk_id = ? AND business_id = ?",
                (cid, business_id),
            ).fetchone()
            if not chunk_row:
                continue
            vector_hits[cid] = (self._row_to_chunk(chunk_row), float(row["distance"]))

        # BM25 side via FTS5
        # Escape single quotes for FTS query safety
        safe_query = query.replace('"', ' ').replace("'", " ")
        try:
            fts_rows = conn.execute(
                """
                SELECT c.chunk_id, c.business_id, c.source, c.kind, c.text, c.metadata,
                       bm25(chunks_fts) AS bm25
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ? AND c.business_id = ?
                ORDER BY bm25 ASC
                LIMIT ?
                """,
                (safe_query, business_id, top_k * 4),
            ).fetchall()
        except sqlite3.OperationalError:
            fts_rows = []

        bm25_hits: dict[str, tuple[Chunk, float]] = {}
        for row in fts_rows:
            chunk = self._row_to_chunk(row)
            # bm25 lower = better; invert
            bm25_score = 1.0 / (1.0 + max(0.0, float(row["bm25"])))
            bm25_hits[chunk.id] = (chunk, bm25_score)

        conn.close()

        # Reciprocal Rank Fusion — combine the two ranked lists
        fused: dict[str, tuple[Chunk, float]] = {}
        vector_rank = {cid: rank for rank, cid in enumerate(
            sorted(vector_hits.keys(), key=lambda cid: vector_hits[cid][1])
        )}
        bm25_rank = {cid: rank for rank, cid in enumerate(
            sorted(bm25_hits.keys(), key=lambda cid: -bm25_hits[cid][1])
        )}
        k_rrf = 60  # standard RRF constant
        all_ids = set(vector_hits) | set(bm25_hits)
        for cid in all_ids:
            score = 0.0
            if cid in vector_rank:
                score += self.alpha * (1.0 / (k_rrf + vector_rank[cid]))
            if cid in bm25_rank:
                score += (1.0 - self.alpha) * (1.0 / (k_rrf + bm25_rank[cid]))
            chunk = vector_hits.get(cid, bm25_hits.get(cid))[0]
            fused[cid] = (chunk, score)

        # Sort and cap
        ranked = sorted(fused.values(), key=lambda t: -t[1])[:top_k]

        # AUDIT FIX 2026-08-01 (RAG-003): use ABSOLUTE RRF ceiling, not
        # top-normalization.  Previous behavior divided by max_score so the
        # top result was ALWAYS 1.0, making the confidence threshold gate
        # meaningless — a hopeless match at position 1 read as 100% confident.
        #
        # RRF ceiling with k_rrf=60: a chunk ranked #1 in BOTH channels scores
        # alpha * 1/61 + (1-alpha) * 1/61 = 1/61 ≈ 0.0164.  We scale by that
        # so a "hit in both channels at rank 1" caps at ~1.0 and everything
        # weaker scales down honestly.
        _RRF_CEIL = 1.0 / (k_rrf + 1)
        return [
            RetrievalHit(
                chunk=chunk,
                score=score,
                confidence=min(1.0, score / _RRF_CEIL),
            )
            for chunk, score in ranked
        ]

    async def size(self, business_id: Optional[str] = None) -> int:
        conn = self._connect()
        self._init_schema(conn)
        if business_id:
            n = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE business_id = ?", (business_id,)
            ).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()
        return int(n)

    # ---- helpers ----

    @staticmethod
    def _hash_id(chunk_id: str) -> int:
        """Deterministic 63-bit int from chunk_id, for use as chunks_vec.rowid."""
        return int(chunk_id, 16) & ((1 << 63) - 1)

    @staticmethod
    def _pack_vector(vec: list[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _row_to_chunk(row) -> Chunk:
        return Chunk(
            text=row["text"],
            business_id=row["business_id"],
            source=row["source"],
            kind=ChunkKind(row["kind"]),
            metadata=json.loads(row["metadata"] or "{}"),
            id=row["chunk_id"],
        )

    @staticmethod
    def _build_rowid_map(conn: sqlite3.Connection, rowids: set[int]) -> dict[int, str]:
        """Build a rowid->chunk_id map for only the requested rowids.

        rowid = _hash_id(chunk_id). We scan the `chunks` table once and
        compute _hash_id() for each row; only rows whose hash is in
        `rowids` land in the returned dict.

        This is O(N) in the size of the KB. For KBs > ~10k chunks we'd
        add a persistent `chunk_hash` column with an index — but at that
        scale you're already on Postgres+pgvector anyway."""
        if not rowids:
            return {}
        result: dict[int, str] = {}
        for r in conn.execute("SELECT chunk_id FROM chunks").fetchall():
            h = SqliteVecRetriever._hash_id(r["chunk_id"])
            if h in rowids:
                result[h] = r["chunk_id"]
                if len(result) == len(rowids):
                    break
        return result
