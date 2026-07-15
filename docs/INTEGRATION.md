# SOPilot Integration Manual (machine-readable edition)

Audience: an integration engineer or a coding agent (e.g. Claude Code) wiring a
product into SOPilot. Everything here is exact and current as of this file's
last commit; the illustrated human version is
[`INTEGRATION.html`](INTEGRATION.html). Architecture rationale lives in
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) — this file is operational only.

## 0. TL;DR integration flows

- **Text channel (simplest):** bootstrap tenant → create project → ingest or
  author SOP → publish → `POST /sessions` → loop `POST /sessions/{id}/converse`
  → `POST .../outcome` + `.../end`.
- **Voice channel:** same until session start, then `POST .../realtime-token`
  (browser connects WebRTC to OpenAI with the ephemeral secret) and per caller
  utterance `POST .../voice-turn` → send `session.update{instructions}` +
  `response.create` over the realtime data channel.
- **Retrieval-only (bring your own agent):** run the project or session with
  `subsystems: "retrieval"`; call `converse`/`plan-turn` per turn and take
  `context_block` into YOUR prompt; ignore `prompt_text`.

## 1. Deployment

```bash
docker compose up -d          # pgvector Postgres :5433, Redis :6380
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env          # set OPENAI_API_KEY, SOPILOT_ADMIN_TOKEN, SOPILOT_SECRET_KEY
.venv/bin/alembic upgrade head
# online lane:
.venv/bin/uvicorn sopilot.api.app:app --host 0.0.0.0 --port 8100        # + SOPILOT_EMBEDDED_SUPERVISOR=true for single-process dev
# background lane (production shape, N replicas):
.venv/bin/sopilot-supervisor
```

Key env vars (`SOPILOT_` prefix; full list in `backend/sopilot/config.py`):

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | postgres on :5433 | asyncpg URL |
| `REDIS_URL` | redis on :6380 | pool + turn-event stream + quotas |
| `ADMIN_TOKEN` | (unset) | required for `POST /admin/tenants` |
| `SECRET_KEY` | (unset → dev key) | Fernet key for tenant connector secrets |
| `SUBSYSTEMS` | `both` | deployment default mode (D-9) |
| `EMBEDDED_SUPERVISOR` | `false` | run one supervisor consumer in-process |
| `RUNTIME_MODEL` | `gpt-4o` | classify/respond model (do NOT downgrade without measuring — documented collapse risk) |
| `BUILDER_MODEL` | `gpt-4o` | SOP ingestion / chat refinement |
| `REALTIME_MODEL` / `REALTIME_VOICE` | `gpt-realtime` / `marin` | voice channel |
| `QUOTA_TURNS_PER_MIN` | `120` | per-tenant fixed-window quota; 0 disables |
| `INSTRUCTION_PREFETCH` | `true` | Milestone-B pre-drafting on/off |

## 2. Auth model

- **Admin plane:** header `X-Admin-Token: <SOPILOT_ADMIN_TOKEN>` — only for
  tenant creation.
- **Everything else:** `Authorization: Bearer sop_<40hex>` (tenant API key,
  sha256-stored) **plus** `X-Project: <project-slug>` on project-scoped routes.
  Keys are tenant-scoped: one key can never see another tenant.
- Error semantics: `401` bad/revoked key · `404` unknown project or object (or
  cross-tenant access — indistinguishable by design) · `409` state conflict
  (unpublished SOP, ended session, duplicate slug) · `422` validation or lint
  failure (body contains `problems[]`) · `429` tenant turn quota exceeded.

## 3. Bootstrap sequence

```bash
# 1. tenant (returns the API key EXACTLY ONCE)
curl -X POST $BASE/admin/tenants -H "X-Admin-Token: $ADMIN" \
  -H 'Content-Type: application/json' -d '{"slug":"acme","name":"Acme"}'
# → {"tenant_id":..., "slug":"acme", "api_key":"sop_..."}

# 2. project (subsystems: "sop" | "retrieval" | "both" | "" = deployment default)
curl -X POST $BASE/admin/projects -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"slug":"collections","subsystems":"both"}'

# change mode later:
curl -X PATCH $BASE/admin/projects/collections -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"subsystems":"sop"}'
```

All subsequent calls: `-H "Authorization: Bearer $KEY" -H "X-Project: collections"`.

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

## 5. Running conversations

### Session lifecycle

```
POST /sessions {"sop_id": ..., "channel": "text"|"realtime_voice",
                "subsystems": ""|"sop"|"retrieval"|"both"}   # per-session D-9 override
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
served verbatim from a pre-draft (no generation happened).

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

## 6. Observability

- `GET /metrics/summary?days=N` — SLIs: `data.speculative_hit_rate` (target
  ≥0.70), `data.live_fallback_rate` (<0.10), `instructions.*` (turn hits vs
  the 70% claim gate), `selection.rerank_ms_p50/p95`, `supervisor_lag_ms`,
  `sessions.by_outcome`.
- `GET /sessions/{id}/pool` — LIVE pool only (cleared at session end; TTL-bound).
- `GET /sessions/{id}/fetches` — permanent per-session prefetch audit
  (served / unused / pending / error per item).
- `GET /sessions` — recent sessions with effective subsystems + outcome.

## 7. Integration invariants (do not violate)

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
   secrets only.
