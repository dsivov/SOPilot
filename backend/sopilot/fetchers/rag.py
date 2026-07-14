"""RAG fetcher over managed pgvector corpora — the production replacement for the
POC's on-disk fixture corpora. Embeddings are computed at ingest; a fetch costs
one query embedding + one indexed KNN query.

Dependency config keys:
    corpus: corpus name within the current project (required)
    top_k:  documents to return (default 3)
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..embeddings import EmbeddingProvider
from ..models import Corpus, CorpusDoc
from ..schemas import DataDependency
from ..tenancy import Scope
from .base import BaseFetcher, FetchOutcome


class PgVectorRagFetcher(BaseFetcher):
    def __init__(self, sessionmaker: async_sessionmaker, embedder: EmbeddingProvider):
        self.sessionmaker = sessionmaker
        self.embedder = embedder

    async def fetch(
        self,
        dep: DataDependency,
        *,
        scope: Scope,
        session_id: str,
        action_name: str,
        query: str | None = None,
    ) -> FetchOutcome:
        cfg = dep.config or {}
        corpus_name = cfg.get("corpus") or ""
        if not corpus_name:
            return FetchOutcome(payload=None, summary=f"<rag: no corpus configured for {dep.name}>")
        if not query:
            text = cfg.get("text") or f"<rag fallback for {dep.name}>"
            return FetchOutcome(payload=text, summary=str(text)[:200])

        q_emb = await self.embedder.embed(query)
        top_k = int(cfg.get("top_k") or 3)
        async with self.sessionmaker() as db:
            corpus = (
                await db.execute(
                    select(Corpus).where(
                        Corpus.tenant_id == scope.tenant_id,
                        Corpus.project_id == scope.project_id,
                        Corpus.name == corpus_name,
                    )
                )
            ).scalar_one_or_none()
            if corpus is None:
                return FetchOutcome(payload=None, summary=f"<rag: corpus '{corpus_name}' not found>")
            rows = (
                await db.execute(
                    select(CorpusDoc.doc_key, CorpusDoc.text)
                    .where(CorpusDoc.corpus_id == corpus.id, CorpusDoc.embedding.isnot(None))
                    .order_by(CorpusDoc.embedding.cosine_distance(q_emb.tolist()))
                    .limit(top_k)
                )
            ).all()
        docs = [(r[0], r[1]) for r in rows]
        joined = "\n\n".join(f"[{doc_key}] {text}" for doc_key, text in docs)
        summary = f"RAG over {dep.name}: {len(docs)} docs for query '{query[:80]}'"
        return FetchOutcome(payload={"docs": docs, "joined_text": joined, "query": query}, summary=summary[:200])
