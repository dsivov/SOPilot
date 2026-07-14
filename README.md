# SOPilot

SOP-based conversation agent management — productization of the
[MCPlanner](https://github.com/dsivov/MCPlanner) research. Multi-tenant,
multi-project, realtime-voice-first.

- Kickoff discussion (I): `docs/BLOG_SOPILOT_KICKOFF.html`
- Architecture discussion (II): `docs/BLOG_SOPILOT_ARCHITECTURE.html`
- Engineer reference: [`ARCHITECTURE.md`](ARCHITECTURE.md) — topology, per-turn
  contract, data model (Mermaid), SLIs, decision log D-1…D-8
- Current phase: **P0 — substrate done** (verified); next: D-1 two-deployable
  split (`sopilot-api` / `sopilot-supervisor` over a Redis Streams turn-event bus)

## Dev setup

```bash
docker compose up -d                      # postgres (pgvector) on :5433, redis on :6380
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env                      # fill OPENAI_API_KEY + SOPILOT_ADMIN_TOKEN
.venv/bin/alembic upgrade head
.venv/bin/uvicorn sopilot.api.app:app --host 127.0.0.1 --port 8100 --reload
```

## Tests

```bash
cd backend
.venv/bin/pytest             # unit tests: no network, fakeredis + fake embeddings
```

## Bootstrap a tenant

```bash
curl -s -X POST localhost:8100/admin/tenants \
  -H "X-Admin-Token: $SOPILOT_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug": "acme", "name": "Acme Corp"}'
# → returns the tenant API key (shown once). Then:
curl -s -X POST localhost:8100/admin/projects \
  -H "Authorization: Bearer sop_..." -H 'Content-Type: application/json' \
  -d '{"slug": "collections"}'
# All scoped calls: Authorization: Bearer sop_...  +  X-Project: collections
```

## Layout

```
backend/sopilot/
  schemas.py      TaskDefinition (SOP JSON schema, ported from MCPlanner, research knobs removed)
  sop_graph.py    allowed-actions semantics + the publish linter
  predictor.py    empirical counting predictor (recency decay + shrinkage) — the workhorse
  pool.py         Redis session pool (misprediction-tolerant blackboard)
  rerank.py       cosine+dedup per-turn curation + the speculative-framing prompt contract
  prefetch.py     schedule/consume lifecycle, cross-worker dedup, Postgres audit
  fetchers/       fetcher SDK: mock, rag (pgvector corpora), mcp (stub)
  scheduler.py    PASTE-style speculative budget + critical-path preemption
  tenancy.py      tenant→project scoping, API keys
  api/            FastAPI app: admin, sops (versioned + lint/publish), sessions (+pool X-ray)
```
