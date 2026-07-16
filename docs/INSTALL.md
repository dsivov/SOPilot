# SOPilot Installation & Configuration Guide

Everything needed to stand up SOPilot on a fresh machine or against existing
infrastructure. The companion documents are `INTEGRATION.md` (API usage after
install) and `../ARCHITECTURE.md` (why the pieces exist).

---

## 1. Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.12+ | backend + workers |
| Node.js | 20+ | Studio UI only (build or dev server) |
| PostgreSQL | 16+ **with pgvector ≥ 0.6** | system of record; pgvector is mandatory (embedding columns) |
| Redis | 7+ | session pool, turn-event stream, quotas — **expendable by design**, no persistence required |
| OpenAI API key | — | runtime + builder + embeddings + (optionally) Realtime voice |

Network shape: the API listens on one port (default 8100). The supervisor
workers make outbound calls only (Postgres, Redis, OpenAI, tenant MCP
connectors). The Studio UI is static files + one API origin.

---

## 2. Databases

### Option A — bundled dev stack (docker compose)

The repo ships a compose file that runs both stores on non-default ports so
they never collide with a host Postgres/Redis:

```bash
docker compose up -d     # pgvector/pgvector:pg16 on 127.0.0.1:5433, redis:7-alpine on 127.0.0.1:6380
```

Data persists in the `sopilot_pgdata` volume. Credentials are
`sopilot/sopilot`, database `sopilot` — matching the default
`SOPILOT_DATABASE_URL`, so no `.env` change is needed for this path.

### Option B — existing PostgreSQL

1. The **pgvector extension must be installable**: package `postgresql-16-pgvector`
   (Debian/Ubuntu) / `pgvector_16` (RHEL/Fedora), or use image
   `pgvector/pgvector:pg16`. Managed services: RDS, Cloud SQL, Azure PG and Neon
   all support `CREATE EXTENSION vector`.
2. Create role + database:

```sql
CREATE ROLE sopilot LOGIN PASSWORD '<strong password>';
CREATE DATABASE sopilot OWNER sopilot;
\c sopilot
CREATE EXTENSION IF NOT EXISTS vector;   -- superuser once; migrations verify it
```

3. Point SOPilot at it (asyncpg driver in the scheme, **not** plain `postgresql://`):

```
SOPILOT_DATABASE_URL=postgresql+asyncpg://sopilot:<password>@db.internal:5432/sopilot
```

Sizing: everything except embeddings is small rows. Budget ~7 KB per corpus
document embedding (1536-dim float32 + row) and one `precedent_traces` row per
conversation turn. A 100k-turn, 10k-document deployment fits comfortably in
the smallest managed tier.

### Option C — existing Redis

Any Redis 7+ instance or cluster endpoint works:

```
SOPILOT_REDIS_URL=redis://:<password>@redis.internal:6379/2
```

Pick a dedicated logical DB (`/2` above) if the instance is shared. SOPilot
uses Redis for three things, **all reconstructible**: the per-session
speculation pool (TTL'd), the `sopilot:events:turns` stream (consumer group
`supervisor`), and per-tenant quota counters. Persistence (AOF/RDB) is
therefore optional; losing Redis costs warm-up, never data. Postgres is the
only store that needs backups.

---

## 3. Fresh install, step by step

```bash
git clone https://github.com/dsivov/SOPilot && cd SOPilot

# 1. stores (skip if using existing ones — see §2)
docker compose up -d

# 2. backend
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 3. configuration
cp .env.example .env      # then edit — see §4 for every key
# minimum to change: OPENAI_API_KEY, SOPILOT_ADMIN_TOKEN, SOPILOT_SECRET_KEY

# 4. schema — creates ALL tables/indexes on an empty database, and is the
#    same command for upgrades later (migrations are additive; run at every deploy)
.venv/bin/alembic upgrade head

# 5. processes
#    dev (single process, supervisor embedded):
SOPILOT_EMBEDDED_SUPERVISOR=true .venv/bin/uvicorn sopilot.api.app:app --host 0.0.0.0 --port 8100
#    production (two deployables, scale independently):
.venv/bin/uvicorn sopilot.api.app:app --host 0.0.0.0 --port 8100   # online lane, N replicas
.venv/bin/sopilot-supervisor                                       # background lane, N replicas

# 6. verify
curl -s http://127.0.0.1:8100/health           # → {"status":"ok", ...}
cd .. && scripts/smoke_test.sh                 # 14-check end-to-end pass (needs OPENAI_API_KEY)

# 7. (optional) load the packaged demo content
backend/.venv/bin/python scripts/seed_demo.py  # prints the demo tenant key when done
```

Multiple supervisor replicas are safe out of the box: they share the Redis
consumer group, each event is delivered to exactly one worker, and stalled
deliveries are reclaimed via `XAUTOCLAIM`.

### Studio UI

```bash
cd frontend && npm install

# dev server (remote-friendly: binds 0.0.0.0, proxies /api → :8100;
# serves HTTPS if frontend/certs/dev.{crt,key} exist, plain HTTP otherwise)
npm run dev                # → https://<host>:5174

# production build — static files for any web server:
npm run build              # → frontend/dist/
```

The UI always calls the **relative path `/api/*`** (no CORS anywhere). In
production, serve `dist/` and add one reverse-proxy rule on the same origin:
`/api/* → http://api-host:8100/*` (strip the `/api` prefix), e.g. nginx:

```nginx
location /api/ { proxy_pass http://127.0.0.1:8100/; }
location /     { root /srv/sopilot/dist; try_files $uri /index.html; }
```

HTTPS matters for one reason: browsers only grant microphone access (voice
channel) to secure origins. Behind a reverse proxy with real TLS you don't
need the self-signed dev cert; for the bare dev server, create one with
`openssl req -x509 -newkey rsa:2048 -nodes -keyout frontend/certs/dev.key -out frontend/certs/dev.crt -days 365 -subj "/CN=sopilot-dev"` and accept the browser warning once.

---

## 4. Configuration reference (`backend/.env`)

All keys use the `SOPILOT_` prefix except `OPENAI_API_KEY` (the OpenAI SDK
reads it from the process environment; SOPilot exports it from `.env` at
startup). Full source of truth: `backend/sopilot/config.py`.

### Required

| Key | Meaning |
|---|---|
| `OPENAI_API_KEY` | used for runtime, authoring, embeddings, voice |
| `SOPILOT_DATABASE_URL` | asyncpg DSN (`postgresql+asyncpg://…`) |
| `SOPILOT_REDIS_URL` | `redis://[:pass@]host:port/db` |
| `SOPILOT_ADMIN_TOKEN` | bearer for `/admin/tenants` (tenant bootstrap). Set a strong value — it mints API keys |
| `SOPILOT_SECRET_KEY` | Fernet key encrypting tenant connector secrets at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Empty = dev fallback key + startup warning. **Rotating it invalidates stored tenant secrets** — they must be re-entered |

### Topology & modes

| Key | Default | Meaning |
|---|---|---|
| `SOPILOT_EMBEDDED_SUPERVISOR` | `false` | run one supervisor consumer inside the API process (dev only) |
| `SOPILOT_SUBSYSTEMS` | `both` | deployment-default mode: `sop` \| `retrieval` \| `both`; overridable per project and per session (D-9) |
| `SOPILOT_QUOTA_TURNS_PER_MIN` | `120` | per-tenant fixed-window turn quota; `0` disables; breach → HTTP 429 |

### Models

| Key | Default | Meaning |
|---|---|---|
| `SOPILOT_RUNTIME_MODEL` | `gpt-4o` | text-channel classify/respond. **Do not downgrade without measuring** — cheap classifiers collapsed task success in our benchmarks |
| `SOPILOT_BUILDER_MODEL` | `gpt-4o` | SOP ingestion + chat refinement (never on the turn path) |
| `SOPILOT_REALTIME_MODEL` / `SOPILOT_REALTIME_VOICE` | `gpt-realtime` / `marin` | voice channel |
| `SOPILOT_EMBEDDING_MODEL` / `SOPILOT_EMBEDDING_DIM` | `text-embedding-3-small` / `1536` | changing dim after data exists requires re-embedding corpora |

### Tuning (validated defaults — change with data, not taste)

| Key | Default | Meaning |
|---|---|---|
| `SOPILOT_SPECULATIVE_BUDGET` | `4` | max concurrent speculative LLM calls per worker |
| `SOPILOT_POOL_MAX_ITEMS` / `SOPILOT_SESSION_TTL_S` | `30` / `7200` | session pool cap / session TTL |
| `SOPILOT_INSTRUCTION_PREFETCH` | `true` | pre-drafted replies (Milestone B) |
| `SOPILOT_INSTRUCTION_PREFETCH_MAX_PERGEN` / `SOPILOT_INSTRUCTION_TTL_S` | `3` / `180` | drafts per turn event / draft TTL |
| `SOPILOT_PREDICTOR_RECENCY_HALF_LIFE_DAYS` | `30` | trace recency decay |
| `SOPILOT_PREDICTOR_SHRINKAGE_KAPPA` / `SOPILOT_PREDICTOR_MIN_SUPPORTING` | `2.0` / `3` | cold-start shrinkage / min evidence |
| `SOPILOT_SUPERVISOR_GROUP` / `_BATCH` / `_BLOCK_MS` / `_AUTOCLAIM_IDLE_MS` | `supervisor`/`10`/`1000`/`60000` | stream consumer knobs |

Never commit `.env` — it is gitignored; keep it that way.

---

## 5. Upgrades

```bash
git pull
cd backend && .venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head        # additive migrations, safe on live data
# restart API replicas, then supervisor replicas (order doesn't matter —
# unprocessed turn events wait in the stream and are consumed after restart)
```

Verification after any upgrade: `scripts/check.sh` (lint + unit tests) and
`scripts/e2e_check.py` (17 live checks against a running deployment).

---

## 6. Operations notes

- **Back up Postgres only.** Redis contents are speculative caches and
  counters; the audit trail (`data_fetches`, `pool_picks`, traces, turns) is
  in Postgres.
- **Health**: `GET /health` on the API. Supervisor lag is exposed at
  `GET /metrics/summary` (`supervisor_lag_ms`) per tenant, and the Dashboard
  view shows it live.
- **Scaling**: API replicas are stateless. Supervisor replicas scale by
  adding processes (shared consumer group). Postgres is the ceiling; the
  online lane holds no LLM calls, so API latency stays flat under load.
- **Tenant bootstrap** (`POST /admin/tenants`) returns the API key exactly
  once — store it; only its SHA-256 is kept server-side.
