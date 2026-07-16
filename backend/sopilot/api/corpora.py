"""Managed RAG corpora: the content side of `rag`-kind connectors (D-10).

Docs are embedded at ingest (one embedding call per doc); a fetch later costs
one query embedding + one indexed KNN query. Changing the deployment embedding
model requires re-ingesting corpora — see INSTALL.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..models import Corpus, CorpusDoc
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/corpora", tags=["corpora"])


class DocIn(BaseModel):
    doc_key: str
    text: str
    topic: str = ""
    tags: list[str] = Field(default_factory=list)


class DocsUpsertRequest(BaseModel):
    docs: list[DocIn] = Field(..., max_length=500)


async def _get(db: AsyncSession, scope: Scope, name: str) -> Corpus:
    row = (
        await db.execute(
            select(Corpus).where(
                Corpus.tenant_id == scope.tenant_id,
                Corpus.project_id == scope.project_id,
                Corpus.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"corpus '{name}' not found")
    return row


@router.get("")
async def list_corpora(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(
            select(Corpus, func.count(CorpusDoc.id))
            .outerjoin(CorpusDoc, CorpusDoc.corpus_id == Corpus.id)
            .where(Corpus.tenant_id == scope.tenant_id, Corpus.project_id == scope.project_id)
            .group_by(Corpus.id)
            .order_by(Corpus.name)
        )
    ).all()
    return [
        {"name": c.name, "docs": int(n), "embedding_model": c.embedding_model, "created_at": c.created_at.isoformat()}
        for c, n in rows
    ]


@router.put("/{name}")
async def create_corpus(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    row = (
        await db.execute(
            select(Corpus).where(
                Corpus.tenant_id == scope.tenant_id,
                Corpus.project_id == scope.project_id,
                Corpus.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Corpus(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            name=name,
            embedding_model=get_settings().embedding_model,
        )
        db.add(row)
        await db.commit()
    return {"name": name, "embedding_model": row.embedding_model}


@router.put("/{name}/docs")
async def upsert_docs(
    name: str,
    req: DocsUpsertRequest,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    corpus = await _get(db, scope, name)
    embedder = request.app.state.embedder
    written = 0
    for d in req.docs:
        if not d.text.strip():
            continue
        emb = await embedder.embed(f"{d.topic}: {d.text}" if d.topic else d.text)
        existing = (
            await db.execute(
                select(CorpusDoc).where(CorpusDoc.corpus_id == corpus.id, CorpusDoc.doc_key == d.doc_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(CorpusDoc(corpus_id=corpus.id, doc_key=d.doc_key, topic=d.topic, tags=d.tags, text=d.text, embedding=emb))
        else:
            existing.topic, existing.tags, existing.text, existing.embedding = d.topic, d.tags, d.text, emb
        written += 1
    await db.commit()
    return {"corpus": name, "docs_written": written}


@router.get("/{name}/docs")
async def list_docs(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db), limit: int = 100
) -> list[dict]:
    corpus = await _get(db, scope, name)
    rows = (
        (
            await db.execute(
                select(CorpusDoc).where(CorpusDoc.corpus_id == corpus.id).order_by(CorpusDoc.doc_key).limit(min(limit, 500))
            )
        )
        .scalars()
        .all()
    )
    return [{"doc_key": d.doc_key, "topic": d.topic, "tags": d.tags, "text": d.text} for d in rows]


@router.delete("/{name}")
async def delete_corpus(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    corpus = await _get(db, scope, name)
    await db.execute(sa_delete(CorpusDoc).where(CorpusDoc.corpus_id == corpus.id))
    await db.delete(corpus)
    await db.commit()
    return {"deleted": name}
