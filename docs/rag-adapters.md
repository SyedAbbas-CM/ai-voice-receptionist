# Bringing your own RAG backend

You have an existing chatbot RAG stack (LangChain / LlamaIndex / Pinecone / Weaviate / Qdrant). This doc shows how to plug it in as one of our `Retriever` backends without abandoning what you already built.

## The interface you have to implement

```python
from packages.rag import Retriever, Chunk, RetrievalHit, ChunkKind

class MyRetriever(Retriever):
    name = "my-backend"

    async def upsert(self, chunks: list[Chunk]) -> int:
        """Idempotent write. Same chunk_id -> same row."""
        ...

    async def search(
        self,
        query: str,
        business_id: str,
        top_k: int = 3,
    ) -> list[RetrievalHit]:
        """Return top_k results. RetrievalHit.confidence in [0, 1]."""
        ...
```

That's it. Everything else — voice shaping, tool wiring, confidence thresholds — happens in this repo's code and doesn't touch your backend.

## Why we ask for `business_id` scoping

Voice agents are multi-tenant. Any query might hit a KB that mixes 100 clients' data if you don't filter. Every implementation MUST filter by `business_id` — leaking a chunk from client A into client B's call is worse than a hallucination.

Most vector DBs support this via a metadata filter:
- Pinecone: `filter={"business_id": {"$eq": business_id}}`
- Weaviate: `where={"path": ["business_id"], "operator": "Equal", "valueText": business_id}`
- pgvector: `WHERE business_id = $1`
- Qdrant: `Filter(must=[FieldCondition(key="business_id", match=MatchValue(value=business_id))])`

## Adapter 1 — LangChain BaseRetriever

If your existing project exposes a `langchain.schema.BaseRetriever`, wrap it:

```python
from langchain.schema import BaseRetriever
from packages.rag import Retriever, Chunk, RetrievalHit, ChunkKind


class LangChainRetrieverAdapter(Retriever):
    """Wraps a LangChain BaseRetriever as a voice-agent Retriever.

    Note: LangChain retrievers usually don't take a business_id filter.
    Pass one at construction, or add a MetadataFilterRetriever wrapper
    in your LangChain graph."""

    name = "langchain"

    def __init__(self, base_retriever: BaseRetriever, business_id: str, embedder=None):
        self.base = base_retriever
        self.pinned_business_id = business_id
        self.embedder = embedder  # unused; LangChain owns embedding

    async def upsert(self, chunks: list[Chunk]) -> int:
        """LangChain retrievers are read-only wrappers over a vector store.
        Route upserts to the underlying vector store directly instead:

            from langchain_pinecone import PineconeVectorStore
            store = PineconeVectorStore(index=..., embedding=...)
            store.add_texts(
                texts=[c.text for c in chunks],
                metadatas=[{"business_id": c.business_id, "source": c.source,
                            "kind": c.kind.value, **c.metadata} for c in chunks],
                ids=[c.id for c in chunks],
            )
        """
        raise NotImplementedError(
            "LangChain adapter is read-only. Use the underlying vector store's "
            "add_texts() for writes."
        )

    async def search(
        self,
        query: str,
        business_id: str,
        top_k: int = 3,
    ) -> list[RetrievalHit]:
        if business_id != self.pinned_business_id:
            # Prevent cross-tenant leakage
            return []

        docs = await self.base.aget_relevant_documents(query)
        docs = docs[:top_k]
        hits = []
        for i, doc in enumerate(docs):
            metadata = doc.metadata or {}
            chunk = Chunk(
                text=doc.page_content,
                business_id=business_id,
                source=metadata.get("source", "langchain"),
                kind=ChunkKind(metadata.get("kind", "other")),
                metadata={k: v for k, v in metadata.items() if k not in ("source", "kind")},
                id=metadata.get("id"),
            )
            # LangChain doesn't return scores by default. Approximate confidence
            # from rank: top-1 = 0.9, top-3 = 0.5.
            rank_confidence = max(0.0, 0.9 - i * 0.2)
            hits.append(RetrievalHit(chunk=chunk, score=1.0 - i * 0.1, confidence=rank_confidence))
        return hits
```

Wire it into the app by editing `session_manager._get_retriever()`:

```python
def _get_retriever():
    if getattr(settings, "rag_retriever", None) == "langchain":
        # Your existing LangChain setup lives elsewhere in the repo or as a
        # dep — import + construct here.
        from my_langchain_project import make_pinecone_retriever
        base = make_pinecone_retriever(index_name="voiceops-clinic-001")
        return LangChainRetrieverAdapter(base, business_id="demo-clinic-001")
    ...
```

## Adapter 2 — Pinecone (direct)

Skip LangChain, talk to Pinecone directly:

```python
from pinecone import Pinecone
from packages.rag import Retriever, Chunk, RetrievalHit, ChunkKind
from packages.rag.embedder import Embedder


class PineconeRetriever(Retriever):
    name = "pinecone"

    def __init__(self, api_key: str, index_name: str, embedder: Embedder, namespace: str | None = None):
        self.pc = Pinecone(api_key=api_key)
        self.index = self.pc.Index(index_name)
        self.embedder = embedder
        self.namespace = namespace

    async def upsert(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        vectors = await self.embedder.embed([c.text for c in chunks])
        payload = [
            {
                "id": c.id,
                "values": v,
                "metadata": {
                    "business_id": c.business_id,
                    "source": c.source,
                    "kind": c.kind.value,
                    "text": c.text,
                    **c.metadata,
                },
            }
            for c, v in zip(chunks, vectors)
        ]
        self.index.upsert(vectors=payload, namespace=self.namespace)
        return len(payload)

    async def search(self, query: str, business_id: str, top_k: int = 3) -> list[RetrievalHit]:
        vec = (await self.embedder.embed([query]))[0]
        res = self.index.query(
            vector=vec,
            top_k=top_k,
            include_metadata=True,
            namespace=self.namespace,
            filter={"business_id": {"$eq": business_id}},
        )
        hits = []
        for m in res.matches:
            meta = m.metadata or {}
            chunk = Chunk(
                text=meta.get("text", ""),
                business_id=business_id,
                source=meta.get("source", "pinecone"),
                kind=ChunkKind(meta.get("kind", "other")),
                id=m.id,
            )
            hits.append(RetrievalHit(
                chunk=chunk,
                score=float(m.score),
                confidence=float(m.score),  # Pinecone returns cosine [-1, 1] or [0, 1] per index metric
            ))
        return hits
```

## Adapter 3 — Supabase / pgvector

Coming as a first-class backend in a follow-up pass — same interface. Meanwhile, the LangChain adapter pattern above works if your existing project uses `langchain_community.vectorstores.SupabaseVectorStore`.

## What NOT to do

- **Don't fetch 20 chunks and stuff them all into the LLM prompt.** That's chatbot RAG. For voice, top-1 with confidence gating is almost always better.
- **Don't skip the business_id filter.** Multi-tenant leakage on a phone call = client fires you.
- **Don't return chunks with URLs / markdown / lists in `text`.** The voice-shaper will reject them and the caller hears silence. Clean at ingest time instead.
- **Don't add your own voice-shaping.** Ours is opinionated on purpose (one sentence, guard against unspeakable output). Trust the pipeline.

## What SUCCESS looks like

You point `RAG_RETRIEVER=langchain` (or `pinecone`, or your custom name once you register it in `build_retriever`), rerun the server, and the browser sim calls `lookup_answer` on caller questions — same Loom demo, same tool-calling brain, same voice-shaper — but backed by your existing production KB with millions of chunks.

Zero brain code changes. That's the point.
