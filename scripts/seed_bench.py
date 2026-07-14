#!/usr/bin/env python3
"""Seed the bench tenant/project/SOP/corpus from the MCPlanner research seeds.

- tenant 'bench' + project 'bench' (mode 'both') via the admin API
- car-insurance-renewal SOP adapted to TaskDefinition, published
- RAG corpus ingested into pgvector with embeddings (direct DB — the corpus
  management API is a P3 console feature)

Usage:
    .venv/bin/python ../scripts/seed_bench.py <path-to-MCPlanner-repo> [base_url]
Prints: API key, project slug, SOP id (feed these to sopilot-bench).
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8100"
ADMIN_TOKEN = "dev-admin-token-p0"


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:400]}


async def ingest_corpus_db(mcp_root: Path, corpus_name: str, project_slug: str, tenant_slug: str) -> int:
    import os

    env_path = Path(__file__).resolve().parents[1] / "backend" / ".env"
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    from sqlalchemy import select

    from sopilot.db import get_sessionmaker
    from sopilot.embeddings import OpenAIEmbeddings
    from sopilot.models import Corpus, CorpusDoc, Project, Tenant

    embedder = OpenAIEmbeddings()
    corpus_dir = mcp_root / "data" / "rag_corpus" / corpus_name
    docs: list[dict] = []
    for f in sorted(corpus_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    if not docs:
        return 0

    texts = [d["text"] for d in docs]
    embeddings = await embedder.embed_many(texts)

    async with get_sessionmaker()() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))).scalar_one()
        project = (
            await db.execute(
                select(Project).where(Project.tenant_id == tenant.id, Project.slug == project_slug)
            )
        ).scalar_one()
        existing = (
            await db.execute(
                select(Corpus).where(Corpus.project_id == project.id, Corpus.name == corpus_name)
            )
        ).scalar_one_or_none()
        if existing:
            return -1  # already ingested
        corpus = Corpus(
            tenant_id=tenant.id, project_id=project.id, name=corpus_name,
            embedding_model=embedder.model,
        )
        db.add(corpus)
        await db.flush()
        for d, emb in zip(docs, embeddings):
            db.add(
                CorpusDoc(
                    corpus_id=corpus.id,
                    doc_key=str(d.get("id")),
                    topic=str(d.get("topic") or ""),
                    tags=d.get("tags") or [],
                    text=d["text"],
                    embedding=emb.tolist(),
                )
            )
        await db.commit()
    return len(docs)


def adapt_sop(raw: dict) -> dict:
    """MCPlanner seed → SOPilot TaskDefinition (drop retired fields, keep the rest)."""
    raw = dict(raw)
    raw.pop("strategies", None)
    # RAG deps: point at the pgvector corpus by name instead of a fixture path.
    for dep in raw.get("data_dependencies", []):
        if dep.get("kind") == "rag":
            cfg = dep.setdefault("config", {})
            cfg["corpus"] = "car_insurance_renewal"
    return raw


def main() -> int:
    mcp_root = Path(sys.argv[1]).resolve()
    seed_path = mcp_root / "data" / "sops" / "car_insurance_renewal.json"
    raw = json.loads(seed_path.read_text())
    definition = adapt_sop(raw)

    t = call("POST", "/admin/tenants", {"slug": "bench"}, {"X-Admin-Token": ADMIN_TOKEN})
    if "_status" in t:
        print(f"tenant 'bench' exists or failed: {t} — create a fresh DB or reuse the printed key from before")
        return 1
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}", "X-Project": "bench"}
    call("POST", "/admin/projects", {"slug": "bench", "subsystems": "both"}, auth)

    sop = call("POST", "/sops", {"definition": definition}, auth)
    if "_status" in sop:
        print("SOP create failed:", sop)
        return 1
    lint = call("POST", f"/sops/{sop['id']}/lint", None, auth)
    pub = call("POST", f"/sops/{sop['id']}/publish", None, auth)
    if "_status" in pub:
        print("publish blocked:", json.dumps(lint, indent=2))
        return 1

    n = asyncio.run(ingest_corpus_db(mcp_root, "car_insurance_renewal", "bench", "bench"))
    print(f"corpus docs ingested: {n}")
    print("\n--- bench credentials ---")
    print(f"API_KEY={key}")
    print("PROJECT=bench")
    print(f"SOP_ID={sop['id']}")
    print(f"lint: {lint['problems'] or 'clean'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
