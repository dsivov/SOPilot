# SOPilot Integration Manual (machine-readable edition)

Audience: the production/integration team (or a coding agent) deploying,
operating, and wiring a product into SOPilot. Everything here is exact and
current as of this file's last commit; the illustrated human version is
[`INTEGRATION.html`](INTEGRATION.html) (may lag this file — this one is
authoritative). The **complete per-endpoint reference** is
[`API_REFERENCE.md`](API_REFERENCE.md), and an interactive Swagger UI is served
at `/docs` on any running instance. Architecture rationale lives in
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) — this file is operational only.

## 0. TL;DR integration flows

- **Text channel (simplest):** bootstrap tenant → create project → ingest or
  author SOP → publish → `POST /sessions` → loop `POST /sessions/{id}/converse`
  → `POST .../outcome` + `.../end`.
- **Voice channel:** same until session start, then `POST .../realtime-token`
  (browser connects WebRTC to OpenAI with the ephemeral secret) and per caller
  utterance `POST .../voice-turn` → send `session.update{instructions}` +
  `response.create` over the realtime data channel.
- **External agent via MCP (e.g. the PolarTie voice platform):** SOPilot serves
  an MCP surface at `/mcp`; the agent adds it to its `mcp_servers` and either
  calls `sop_guidance` per turn (model-driven) or lets the platform's
  supervisor extension auto-drive the reserved `polartie_ai_agent_supervisor`
  tool. **This is the AENA production shape — see §9.**
- **Retrieval-only (bring your own agent):** run the project or session with
  `subsystems: "retrieval"`; call `converse`/`plan-turn` per turn and take
  `context_block` into YOUR prompt; ignore `prompt_text`.
- **No-curl path:** everything in §3–§4 can be done from the Studio UI — the
  platform admin console (tenants, keys, export/import) and the tenant Studio
  (SOPs, prompt blocks, connectors). See §7.

## 1. Deployment

> Full installation guide — requirements, existing vs fresh Postgres/Redis
> (pgvector, DSNs, sizing), complete `.env` reference, Studio production build
> + reverse proxy, upgrades, backups: **`docs/INSTALL.md`**. Below is the
> operational shape.

```bash
docker compose up -d          # pgvector Postgres :5433, Redis :6380
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env          # set OPENAI_API_KEY, SOPILOT_ADMIN_TOKEN, SOPILOT_SECRET_KEY
.venv/bin/alembic upgrade head

# online lane — production entrypoint; host/port from env:
SOPILOT_HOST=0.0.0.0 SOPILOT_PORT=8100 .venv/bin/sopilot-api
# (dev equivalent: .venv/bin/uvicorn sopilot.api.app:app --port 8100 --reload
#  + SOPILOT_EMBEDDED_SUPERVISOR=true for single-process dev)

# background lane (production shape, N replicas):
.venv/bin/sopilot-supervisor
```

Studio UI (dev server / preview; production is a static `npm run build` behind
your reverse proxy):

```bash
cd frontend && npm install
SOPILOT_UI_PORT=5174 SOPILOT_API_URL=http://127.0.0.1:8100 npm run dev
# binds 0.0.0.0, HTTPS if certs/dev.{crt,key} exist (required for mic access
# from a remote origin); /api is proxied to SOPILOT_API_URL
```

Key env vars (`SOPILOT_` prefix; full list in `backend/sopilot/config.py`):

| Var | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8100` | API bind for the `sopilot-api` entrypoint |
| `DATABASE_URL` | postgres on :5433 | asyncpg URL |
| `REDIS_URL` | redis on :6380 | pool + turn-event stream + quotas |
| `ADMIN_TOKEN` | (unset) | the platform-admin credential (§2) — REQUIRED in prod |
| `SECRET_KEY` | (unset → dev key) | Fernet key for tenant connector secrets |
| `SUBSYSTEMS` | `both` | deployment default mode (D-9) |
| `EMBEDDED_SUPERVISOR` | `false` | run one supervisor consumer in-process (dev) |
| `MCP_MOUNT` | `false` | serve the MCP surface at `/mcp` in-process (§8) |
| `MCP_MODE` | `both` | which MCP tools to expose: `tool` \| `supervisor` \| `both` |
| `RUNTIME_MODEL` | `gpt-4o` | classify/respond model (do NOT downgrade without measuring — documented collapse risk) |
| `RESPOND_MODEL` | (empty = runtime) | the voice the caller hears; can be small — the supervisor carries the procedure |
| `ROUTER_MODEL` | `gpt-4o-mini` | D-11 intake routing + mid-call switch checks |
| `BUILDER_MODEL` | `gpt-4o` | SOP ingestion / chat refinement / config-rule drafting |
| `REALTIME_MODEL` / `REALTIME_VOICE` | `gpt-realtime` / `marin` | voice channel |
| `QUOTA_TURNS_PER_MIN` | `120` | per-tenant fixed-window quota; 0 disables |
| `INSTRUCTION_PREFETCH` | `true` | Milestone-B pre-drafting on/off |

UI env (read by `frontend/vite.config.ts`, dev + preview):

| Var | Default | Meaning |
|---|---|---|
| `SOPILOT_UI_PORT` | `5174` | Studio port |
| `SOPILOT_API_URL` | `http://127.0.0.1:8100` | backend the `/api` proxy forwards to |

## 2. Auth model

- **Admin plane:** header `X-Admin-Token: <SOPILOT_ADMIN_TOKEN>` — the
  deployment operator's credential. Guards tenant CRUD, per-tenant key
  management, one-click login-key minting, and admin-side project
  export/import (§5–§7). Held only by whoever runs the instance; never by a
  tenant.
- **Everything else:** `Authorization: Bearer sop_<40hex>` (tenant API key,
  sha256-stored — the raw key exists only at mint time) **plus**
  `X-Project: <project-slug>` on project-scoped routes. Keys are
  tenant-scoped: one key can never see another tenant. Key roles: `runtime`
  (integration traffic) and `admin` (tenant administration); revocation is
  immediate.
- Error semantics: `401` bad/revoked key · `403` bad admin token · `404`
  unknown project or object (or cross-tenant access — indistinguishable by
  design) · `409` state conflict (unpublished SOP, ended session, duplicate
  slug) · `422` validation or lint failure (body contains `problems[]`) ·
  `429` tenant turn quota exceeded.

## 3. Bootstrap sequence

Everything below is also available point-and-click in the **admin console**
(§7) — the curl path is for automation.

```bash
# 1. tenant (returns the API key EXACTLY ONCE)
curl -X POST $BASE/admin/tenants -H "X-Admin-Token: $ADMIN" \
  -H 'Content-Type: application/json' -d '{"slug":"acme","name":"Acme"}'
# → {"tenant_id":..., "slug":"acme", "api_key":"sop_..."}

# 2. project (subsystems: "sop" | "retrieval" | "both" | "advisory" | "" = deployment default)
curl -X POST $BASE/admin/projects -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"slug":"collections","subsystems":"both"}'

# change mode later (also a dropdown in the Studio topbar):
curl -X PATCH $BASE/admin/projects/collections -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"subsystems":"sop"}'
```

All subsequent calls: `-H "Authorization: Bearer $KEY" -H "X-Project: collections"`.

**Subsystem modes** (D-9/D-13, most specific wins — deployment default →
project → per-session override):

| Mode | SOP management | Background retrieval | What runs |
|---|---|---|---|
| `both` (default) | ✔ | ✔ | full system: gated per-turn contract + prediction/prefetch |
| `sop` | ✔ | ✖ | prompt/instruction management with live data resolution, no speculative retrieval |
| `retrieval` | ✖ | ✔ | prediction + prefetch + per-turn `context_block` only — bring your own agent |
| `advisory` | ✔ (full-SOP steering) | ✔ (off the reply path) | responder gets full SOP text + fresh data in one call; classification/tracking run OFF the reply path — the low-latency knowledge-delivery mode (AENA runs this) |

Enable/disable is exactly this switch: the Studio **topbar dropdown** (per
project, live) or the `PATCH /admin/projects/{slug}` call above; per-session
override via `POST /sessions {"subsystems": ...}`.

**Key management (admin plane):**

```bash
curl $BASE/admin/tenants -H "X-Admin-Token: $ADMIN"                        # list + counts
curl $BASE/admin/tenants/acme/keys -H "X-Admin-Token: $ADMIN"              # list keys (hash prefixes only)
curl -X POST $BASE/admin/tenants/acme/keys -H "X-Admin-Token: $ADMIN" \
  -d '{"label":"prod","role":"runtime"}'                                   # mint (raw key returned ONCE)
curl -X POST $BASE/admin/tenants/acme/keys/{key_id}/revoke -H "X-Admin-Token: $ADMIN"
curl -X DELETE $BASE/admin/tenants/acme -H "X-Admin-Token: $ADMIN"         # full cascade delete
```

## 4. Authoring SOPs

| Endpoint | Purpose |
|---|---|
| `POST /sops/ingest` `{text, name_hint?}` | policy text → draft SOP (LLM), returns `{id, definition, lint}` |
| `POST /sops/ingest-file` (multipart `file`, `name_hint`) | PDF/txt/md upload → same pipeline; 2 MB cap |
| `POST /sops/build-turn` `{history, current_definition}` | one conversational refinement turn; STATELESS — returns `{assistant_message, definition, lint}`; client saves explicitly |
| `POST /sops/lint-definition` `{definition}` | stateless lint (for live editors) |
| `POST /sops` / `PUT /sops/{id}` | create / save-new-version (draft) |
| `POST /sops/{id}/lint` · `POST /sops/{id}/publish` | lint gate; publish 422s while problems exist (incl. unpublished bound prompt blocks) |
| `GET /sops` · `GET /sops/{id}` · `DELETE /sops/{id}` | list / read (incl. `source_document` provenance) / delete |

The SOP schema (`TaskDefinition`) is documented inline in
`backend/sopilot/schemas.py`; essentials: `agent_actions[]` (stages, with
`must_say[]`, `data_dependencies[]`, `prompt_blocks[]`), `user_states[]`,
`conversation_profile.success_markers/failure_markers` (terminal state names),
`data_dependencies[]` (kind: `mock|rag|kg|db|api|mcp`, `idempotent` — **set
false for anything mutating**; non-idempotent deps are never prefetched),
`sop.edges[]` (`action→action` forward = hard ordering; `state→action` forward
= trigger).

### Prompt blocks (approved wording, versioned separately)

`POST /prompt-blocks` `{name, kind: stage|compliance|role|escalation, content}`
(new draft version) · `POST /prompt-blocks/{name}/publish` · `GET /prompt-blocks`
· `DELETE /prompt-blocks/{name}`. Bind by listing block names in an action's
`prompt_blocks[]`. Rules: SOP publish fails if a bound block has no published
version; bindings are resolved and **pinned at session start** (a block
published mid-conversation never changes a running call).

### Data dependencies of kind `mcp`

```json
{"name": "kb", "kind": "mcp", "idempotent": true, "expected_latency_ms": 1500,
 "config": {"server": "https://kg.example.com/mcp", "tool": "query_knowledge_graph",
             "args": {"mode": "hybrid"}, "query_arg": "query",
             "auth_secret": "kr_api_key", "auth_header": "X-API-Key"},
 "query_template": "customer asks: {user_text}"}
```

Store the credential once per tenant: `PUT /secrets {"name":"kr_api_key",
"value":"..."}` (reads return names only; Fernet-encrypted at rest).

### Connectors — the production shape (D-10)

Inline config works, but production deployments should register retrieval
systems as **named connectors** and have SOP stages bind by name — then a
system can be swapped, re-credentialed, or disabled without republishing any
SOP:

```bash
# register once per project (kinds: mcp | rag | http | mock)
curl -X PUT $BASE/connectors/kb -H "$AUTH" -H "$PROJ" -d '{
  "kind": "mcp", "description": "knowledge graph",
  "config": {"server":"https://kg.example.com/mcp","tool":"query_knowledge_graph",
             "query_arg":"query","auth_secret":"kr_api_key","auth_header":"X-API-Key"}}'

# probe it live (one real fetch; nothing pools, nothing audits)
curl -X POST $BASE/connectors/kb/test -H "$AUTH" -H "$PROJ" -d '{"query":"test"}'
# → {"ok": true, "latency_ms": 240, "summary": "…", "payload_excerpt": "…"}

# monitor: registry + 7-day health (fetch volume, error rate, p50/p95 latency,
# SOPs binding each connector) — the Studio Connectors view renders this
curl "$BASE/connectors?days=7" -H "$AUTH" -H "$PROJ"
```

Bind from an SOP stage by name only; tuning keys may override the connector's
defaults (the dependency's `kind` is replaced by the connector's at fetch time):

```json
{"name": "kb_lookup", "kind": "mock", "idempotent": true,
 "config": {"connector": "kb", "top_k": 2},
 "query_template": "customer asks: {user_text}"}
```

The generic `http` kind covers RAG endpoints and internal search/tool APIs —
config keys: `url`, `method` (GET/POST), `query_field`, `body`, `params`,
`headers`, `auth_secret`, `auth_header`, `result_path` (dot-path into the
response JSON), `timeout_s`. An unknown or disabled connector makes that fetch
fail *visibly* (audited with the reason, shown in the Connectors health view)
while the turn degrades gracefully — a retrieval outage never crashes a
conversation.

## 5. Project export / import (backup · restore · env promotion)

A project's full authored configuration — **SOPs + prompt blocks +
connectors** — travels as one JSON bundle (`kind:
"sopilot-project-export"`). Connector **secrets never leave the deployment**
(they live in `tenant_secrets`; re-enter them via `PUT /secrets` after a
cross-deployment import).

```bash
# tenant-key plane (Studio topbar has Export/Import buttons for the same):
curl $BASE/project/export -H "$AUTH" -H "$PROJ" > acme-collections.json
curl -X POST $BASE/project/import -H "$AUTH" -H "$PROJ" -d @acme-collections.json

# admin plane (admin console has the same per project + an "Import bundle…" flow):
curl $BASE/admin/tenants/acme/projects/collections/export -H "X-Admin-Token: $ADMIN"
curl -X POST $BASE/admin/tenants/acme/projects/collections/import \
  -H "X-Admin-Token: $ADMIN" -d @acme-collections.json
```

Semantics:

- Export takes the **latest version** of each SOP / block with its status.
- Import **upserts by name**: existing items get a new version, missing ones
  are created; items the bundle marks `published` are re-published through the
  normal lint + prompt-block gate — a failure downgrades that item to draft
  with a warning in the response, it never aborts the import.
- Re-importing the same bundle is an update, not a duplicate (verified
  round-trip: export → import → export is byte-equal on content).
- **Admin plane only:** a missing tenant and/or project is **created
  automatically** (name/subsystems seeded from the bundle's `project` block) —
  this is the fresh-deployment restore path. Mint keys afterwards from the
  console.
- Response: `{"summary": {sops|prompt_blocks|connectors: {created, updated,
  published}}, "warnings": [...]}`.

## 6. Running conversations

### Session lifecycle

**Intake mode (D-11):** start a session with NO `sop_id` and SOPilot's router
assigns the procedure from the conversation itself — deferring politely on
greetings, auditing every decision:

```bash
curl -X POST $BASE/sessions -H "$AUTH" -H "$PROJ" -d '{}'   # → {"routed": false, ...}
# converse as normal; the response carries "routing" when a decision lands:
#   {"routing": {"kind": "initial", "sop_id": "...", "reason": "lost luggage inquiry"}}
# journey exposes the full routing_events audit trail per session.
# advisory mode also supports MID-CALL switching when the caller changes topic.
```

Pass an explicit `sop_id` when the upstream system already knows the intent
(IVR menu choice, app deep link) — explicit selection always wins.

```
POST /sessions {"sop_id": ..., "channel": "text"|"realtime_voice",
                "subsystems": ""|"sop"|"retrieval"|"both"|"advisory"}   # per-session D-9 override
→ {"session_id", "sop_version", "definition"}
...turns...
POST /sessions/{id}/outcome {"outcome":"success"|"failure"|"abandoned"}  # trains the predictor
POST /sessions/{id}/end
```

Requires a **published** SOP version (409 otherwise). Always send `outcome`
before `end` when you know it — terminal rewards are what make prediction
improve.

### Text channel — `POST /sessions/{id}/converse`

Request `{"user_message": "..."}`. Response:

```json
{"reply": "...",                         // say this to the user
 "terminal": null | "success"|"failure", // explicit user closing detected
 "classification": {"cohort","state","mood","action"},
 "turn": {"turn_index","chosen_action","allowed_actions","subsystems",
           "prompt_text","context_block","instruction_hit","picks":[...],
           "consume_stats":{"consumed","live","latency_hidden_ms",...},"rerank_ms"},
 "total_ms": 2100}
```

On `terminal`, call outcome+end. `instruction_hit=true` means the reply was
served verbatim from a pre-draft (no generation happened). With
`{"steer_only": true}` in the request, SOPilot skips its own responder LLM
call and returns only the steering (`turn.prompt_text`) — the shape the MCP
surface uses (§8).

### Lower-level — `POST /sessions/{id}/plan-turn`

For callers that do their own classification/response: body
`{user_message, cohort?, mood?, state?, action?, prev_assistant_message?}`;
returns the `turn` object above without generating a reply. Supplying an
`action` not currently SOP-legal → 422 with `allowed`.

### Voice channel

1. `POST /sessions/{id}/realtime-token` → `{client_secret, model,
   webrtc_url_ga, webrtc_url_beta, api_flavor}`. Server-minted, manual turn
   control preconfigured (`create_response=false`) with input transcription.
2. Browser/edge: WebRTC SDP exchange to the returned URL with
   `Authorization: Bearer <client_secret>`; data channel `oai-events`.
3. Per final user transcript: `POST /sessions/{id}/voice-turn`
   `{user_message, prev_assistant_message}` → `{instructions, terminal,
   classification, turn, plan_ms}`; then over the data channel send
   `{"type":"session.update","session":{"type":"realtime","instructions":...}}`
   followed by `{"type":"response.create"}`.
4. Reference client: `frontend/src/views/voiceCall.ts` (~150 lines, portable).

### Retrieval-only integration (bring your own agent)

Run with `subsystems:"retrieval"`. Per turn, `converse`/`plan-turn` return
`context_block` (the speculative pre-staged context, honestly framed) and empty
`prompt_text`. Insert `context_block` into your own prompt verbatim — do not
strip its framing header; the "may or may not fit" wording is measured
behavior, not boilerplate.

## 7. Studio & admin console (the no-curl path)

One UI, two entrances (dev: `https://<host>:5174`):

**Tenant Studio** — connect with a tenant key + project. Views: SOPs
(ingestion, JSON editor with live lint, chat refinement, publish gate), Prompt
blocks (versioned library), Connectors (registry + live test + health),
Dashboard/Playground/Sessions/Traces (ops). Topbar: **project switcher**,
**subsystems mode dropdown** (SOP + retrieval / SOP only / Retrieval only /
Advisory — PATCHes the project live), **Export / Import** (the §5 bundle).

**Platform admin console** — "Platform admin →" on the connect screen; needs
`SOPILOT_ADMIN_TOKEN`. Manages RBAC and lifecycle:

- create / delete tenants (delete cascades everything);
- per-tenant key management — mint (`runtime`/`admin` role, label), revoke;
  the raw key is shown exactly once at mint;
- **one-click tenant login** — "Log in →" mints a hidden `console-login` key
  (previous one is hard-deleted; exactly one active) and drops you into that
  tenant's Studio without ever displaying a key;
- per-project **Export / Import**, plus console-level **"Import bundle…"**
  that creates a missing tenant/project on the fly (§5).

## 8. MCP surface — plugging an external agent in (D-11/D-13)

SOPilot can serve MCP itself, so an external voice/chat platform consults the
procedure engine per turn without any SDK work.

**Two deployment shapes:**

```bash
# (a) in-process mount — production; shares app.state, no localhost hop:
SOPILOT_MCP_MOUNT=true SOPILOT_MCP_MODE=supervisor \
SOPILOT_API_KEY=sop_... SOPILOT_PROJECT=<project> .venv/bin/sopilot-api
# → MCP served at https://<sopilot-host>/mcp   (single-tenant surface: the
#   env key+project define whose sessions it runs)

# (b) standalone sidecar (wraps the HTTP API; "add the server and it works"):
SOPILOT_API_KEY=sop_... SOPILOT_PROJECT=<project> python -m sopilot.mcp_server
# env: SOPILOT_BASE_URL (default http://127.0.0.1:8100), SOPILOT_SOP_ID
# (default "" = intake router), SOPILOT_CHANNEL (realtime_voice),
# SOPILOT_SUBSYSTEMS (advisory), SOPILOT_MCP_HOST/PORT/PATH (127.0.0.1/8140//mcp)
```

**Tools exposed** (`SOPILOT_MCP_MODE`):

| Mode | Tool | Who calls it |
|---|---|---|
| `tool` | `sop_guidance(user_message)` | the agent's MODEL decides to call it each turn and follows the returned stage steering in its own voice (Stage 1 — works on a stock platform: just an `mcp_servers` entry) |
| `supervisor` | `polartie_ai_agent_supervisor` (reserved name) | the PLATFORM auto-drives it per turn (client-driven turns); the model never sees a tool to call (Stage 2 — needs the platform's supervisor extension) |
| `both` (default) | both | mixed/dev |

Session mapping: one SOPilot session per MCP connection; the first call opens
the session (no `sop_id` → intake router picks the SOP from the first
utterance; advisory mode also switches SOP mid-call on topic change). Turns run
with `steer_only=true` — SOPilot returns steering text only and never spends
its own responder call.

## 9. AENA production configuration (the live customer deployment)

The current state of the first production customer — use it as the reference
worked example. Handover package manifest:
[`../use_case/delivery/MANIFEST.md`](../use_case/delivery/MANIFEST.md);
measured results: `REPORT_AENA_PROD.html`; onboarding recipe used to build it:
[`ONBOARDING.md`](ONBOARDING.md).

| Item | Value |
|---|---|
| Tenant / project | `aena` ("AENA — Malaga Airport") / `malaga` |
| Subsystems mode | **`advisory`** — knowledge-delivery Q&A; measured: quality class of the best prompt agent, +14pp coverage / +27pp concreteness from SOPs, ~40% lower median latency |
| SOPs (4, published) | lost/delayed luggage · flight check-in & boarding · ground transport, parking & wayfinding · airport services & facilities |
| Prompt blocks (7) | placed config-driven via `prompt_bindings` (24 bindings across all stages); measured satisfaction 4.06→4.38 |
| Knowledge | 111-fact corpus (`airport-facts` connector, rag→corpus) + `flight-status` (mock — live feed is Tier-1 roadmap) |
| Integration | PolarTie voice robot ("Carmen") ↔ SOPilot via **MCP supervisor mode** (§8a) |

**How the robot is wired** (the real robot `config.json` is gitignored —
credentials — and delivered by side channel; a sanitized copy is committed as
`frontend/src/config/exampleConfig.json`, which the Studio Config viewer
renders as the "Example (real)" preset):

1. SOPilot side (we run it): `SOPILOT_MCP_MOUNT=true
   SOPILOT_MCP_MODE=supervisor SOPILOT_API_KEY=<aena runtime key>
   SOPILOT_PROJECT=malaga` → MCP at `https://<sopilot-host>/mcp`.
2. Robot side (PolarTie config): add to the config's `mcp_servers`:
   `{"url": "https://<sopilot-host>/mcp", "authorization": "Bearer <token>"}`.
   With the platform's supervisor extension (`d-sop-stage2-supervisor-ext`
   branches, see `POLARTIE_LETTER.md`), the reserved
   `polartie_ai_agent_supervisor` tool is auto-driven each turn — the robot's
   model follows SOPilot's stage steering in its own voice. For a Stage-1
   trial on a stock platform, flip `SOPILOT_MCP_MODE=tool` (exposes
   `sop_guidance` for model-driven calls).
3. The robot keeps its own KB MCP servers (airport knowledge + schedule) —
   SOPilot does not replace them; it carries the procedures and steering.

Rebuild/restore: the whole `malaga` project travels as one export bundle (§5) —
`GET /admin/tenants/aena/projects/malaga/export` — and imports into a fresh
deployment with tenant+project auto-created.

## 10. Observability

- `GET /metrics/summary?days=N` — SLIs: `data.speculative_hit_rate` (target
  ≥0.70), `data.live_fallback_rate` (<0.10), `instructions.*` (turn hits vs
  the 70% claim gate), `selection.rerank_ms_p50/p95`, `supervisor_lag_ms`,
  `sessions.by_outcome`.
- `GET /sessions/{id}/pool` — LIVE pool only (cleared at session end; TTL-bound).
- `GET /sessions/{id}/fetches` — permanent per-session prefetch audit
  (served / unused / pending / error per item).
- `GET /sessions` — recent sessions with effective subsystems + outcome.
- `GET /health` — liveness (`{"status":"ok"}`).

## 11. Integration invariants (do not violate)

1. Never put an LLM call or blocking lookup between receiving a user utterance
   and calling converse/voice-turn — the runtime owns that path.
2. Treat `context_block` as optional advice for YOUR agent; never re-label it
   as verified/high-relevance.
3. `idempotent:false` on every mutating data dependency — the scheduler trusts
   the flag.
4. Report `outcome` on every session you can; the predictor's quality is your
   tenant's accumulated history.
5. Keys: tenant API keys are shown once; store hashed or in a secret manager.
   The OpenAI key lives only server-side; browsers get ephemeral realtime
   secrets only. `SOPILOT_ADMIN_TOKEN` is the operator's credential — never
   give it to a tenant.
6. Connector secrets don't travel in export bundles — re-enter them via
   `PUT /secrets` after a cross-deployment restore.
