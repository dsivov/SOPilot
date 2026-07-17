# SOPilot API Reference

Complete endpoint reference — **54 endpoints**, generated from the live OpenAPI spec (`docs/openapi.json`). Regenerate with `python scripts/gen_api_reference.py`. The interactive version is served at `/docs` (Swagger UI) on any running instance; task-oriented flows are in [`INTEGRATION.md`](INTEGRATION.md).

## Authentication

- **Admin plane** (`/admin/tenants`): header `X-Admin-Token: <SOPILOT_ADMIN_TOKEN>`.
- **Everything else**: `Authorization: Bearer sop_<key>` + `X-Project: <slug>` on project-scoped routes. Keys are tenant-scoped.
- Errors: `401` bad key · `404` unknown/cross-tenant · `409` state conflict · `422` validation/lint (`problems[]`) · `429` quota.


## Health

### `GET /health`

Health

**Responses:** `200`


## Admin — tenants & projects

### `GET /admin/projects`

List Projects

**Parameters:** `authorization` (header)

**Responses:** `200`

### `POST /admin/projects`

Create Project

**Parameters:** `authorization` (header)

**Request body** (`application/json`): `ProjectCreateRequest`
| field | type | required | notes |
|---|---|---|---|
| `slug` | string | yes |  |
| `name` | string |  | (default ``) |
| `subsystems` | string |  | (default ``) |

**Responses:** `200`

### `PATCH /admin/projects/{slug}`

Update Project

**Parameters:** `slug` (path, required), `authorization` (header)

**Request body** (`application/json`): `ProjectUpdateRequest`
| field | type | required | notes |
|---|---|---|---|
| `subsystems` | string | yes |  |

**Responses:** `200`

### `POST /admin/tenants`

Create Tenant

**Parameters:** `X-Admin-Token` (header)

**Request body** (`application/json`): `TenantCreateRequest`
| field | type | required | notes |
|---|---|---|---|
| `slug` | string | yes |  |
| `name` | string |  | (default ``) |

**Responses:** `200`

### `GET /admin/whoami`

Whoami

**Parameters:** `authorization` (header)

**Responses:** `200`


## SOP authoring

### `GET /sops`

List Sops

**Parameters:** `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sops`

Create Sop

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `SopSaveRequest`
| field | type | required | notes |
|---|---|---|---|
| `definition` | TaskDefinition | yes |  |

**Responses:** `200`

### `POST /sops/build-turn`

One conversational refinement turn. Stateless: the Studio holds the working
definition and saves explicitly (PUT) when the author is happy.

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `BuildTurnRequest`
| field | type | required | notes |
|---|---|---|---|
| `history` | array | yes |  |
| `current_definition` | object | yes |  |

**Responses:** `200`

### `POST /sops/ingest`

Document → draft SOP. Creates the SOP as a draft and returns it with lint results.

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `IngestRequest`
| field | type | required | notes |
|---|---|---|---|
| `text` | string | yes |  |
| `name_hint` | string |  | (default ``) |
| `source_filename` | string |  | (default ``) |

**Responses:** `200`

### `POST /sops/ingest-file`

Document upload (PDF / txt / md) → draft SOP. Same pipeline as /ingest.

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`multipart/form-data`): `Body_ingest_file_sops_ingest_file_post`
| field | type | required | notes |
|---|---|---|---|
| `file` | string | yes |  |
| `name_hint` | string |  | (default ``) |

**Responses:** `200`

### `POST /sops/lint-definition`

Stateless lint for the Studio editor (continuous linting on every change).

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `LintDefinitionRequest`
| field | type | required | notes |
|---|---|---|---|
| `definition` | object | yes |  |

**Responses:** `200`

### `DELETE /sops/{sop_id}`

Delete Sop

**Parameters:** `sop_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /sops/{sop_id}`

Latest version by default; ?version=N pins one (A/B arms use this).

**Parameters:** `sop_id` (path, required), `version` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `PUT /sops/{sop_id}`

Update Sop

**Parameters:** `sop_id` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `SopSaveRequest`
| field | type | required | notes |
|---|---|---|---|
| `definition` | TaskDefinition | yes |  |

**Responses:** `200`

### `POST /sops/{sop_id}/lint`

Lint Sop

**Parameters:** `sop_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sops/{sop_id}/publish`

Publish Sop

**Parameters:** `sop_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /sops/{sop_id}/versions`

List Sop Versions

**Parameters:** `sop_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`


## Prompt blocks

### `GET /prompt-blocks`

List Blocks

**Parameters:** `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /prompt-blocks`

Create the block or append a new draft version to an existing one.

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `BlockSaveRequest`
| field | type | required | notes |
|---|---|---|---|
| `name` | string | yes |  |
| `content` | string | yes |  |
| `kind` | string |  | (default `stage`) |

**Responses:** `200`

### `POST /prompt-blocks/rewrite`

LLM-assisted rewrite (builder model). Stateless preview — nothing is
saved; the client saves the result as a new draft version explicitly.

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `BlockRewriteRequest`
| field | type | required | notes |
|---|---|---|---|
| `content` | string | yes |  |
| `instruction` | string |  | (default ``) |
| `kind` | string |  | (default `stage`) |

**Responses:** `200`

### `DELETE /prompt-blocks/{name}`

Delete Block

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /prompt-blocks/{name}`

Get Block

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /prompt-blocks/{name}/publish`

Publish Block

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`


## Connectors (retrieval systems)

### `GET /connectors`

Registry + health: fetch volume, error rate and latency percentiles from
the audit trail, plus how many SOPs bind each connector.

**Parameters:** `days` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `DELETE /connectors/{name}`

Delete Connector

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `PUT /connectors/{name}`

Save Connector

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `ConnectorSaveRequest`
| field | type | required | notes |
|---|---|---|---|
| `kind` | string | yes |  |
| `description` | string |  | (default ``) |
| `config` | object |  |  |
| `enabled` | boolean |  | (default `True`) |

**Responses:** `200`

### `POST /connectors/{name}/test`

Fire ONE live fetch through the real fetcher with a synthetic dependency.
Nothing pools, nothing audits — this is the operator's connectivity probe.

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `ConnectorTestRequest`
| field | type | required | notes |
|---|---|---|---|
| `query` | string |  | (default `connectivity test — say hello`) |

**Responses:** `200`


## Corpora (managed knowledge)

### `GET /corpora`

List Corpora

**Parameters:** `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `DELETE /corpora/{name}`

Delete Corpus

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `PUT /corpora/{name}`

Create Corpus

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /corpora/{name}/docs`

List Docs

**Parameters:** `name` (path, required), `limit` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `PUT /corpora/{name}/docs`

Upsert Docs

**Parameters:** `name` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `DocsUpsertRequest`
| field | type | required | notes |
|---|---|---|---|
| `docs` | array | yes |  |

**Responses:** `200`


## Tenant secrets

### `GET /secrets`

List Secrets

**Parameters:** `authorization` (header)

**Responses:** `200`

### `PUT /secrets`

Put Secret

**Parameters:** `authorization` (header)

**Request body** (`application/json`): `SecretPutRequest`
| field | type | required | notes |
|---|---|---|---|
| `name` | string | yes |  |
| `value` | string | yes |  |

**Responses:** `200`

### `DELETE /secrets/{name}`

Delete Secret

**Parameters:** `name` (path, required), `authorization` (header)

**Responses:** `200`


## Sessions & conversations

### `GET /sessions`

List Sessions

**Parameters:** `limit` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sessions`

Start Session

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `SessionStartRequest`
| field | type | required | notes |
|---|---|---|---|
| `sop_id` | string |  | (default ``) |
| `channel` | `text` \| `realtime_voice` \| `bench` |  | (default `text`) |
| `subsystems` | `` \| `sop` \| `retrieval` \| `both` \| `advisory` |  | (default ``) |
| `sop_version` | integer |  | (default `0`) |

**Responses:** `200`

### `POST /sessions/{session_id}/converse`

Full text-channel turn: classify+propose (one strong-model call) →
plan-turn (pool, prompts, event) → respond from the instruction payload.
The voice channel reuses everything except the respond step.

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `ConverseRequest`
| field | type | required | notes |
|---|---|---|---|
| `user_message` | string | yes |  |

**Responses:** `200`

### `POST /sessions/{session_id}/end`

End Session

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /sessions/{session_id}/fetches`

The permanent record of what the supervisor did for this session — unlike
the live pool (which is cleared at session end and TTL-bound), this survives.

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /sessions/{session_id}/journey`

The conversation mapped onto its SOP graph: the pinned definition, every
turn (with the state/action the tracker assigned), and the prompt-block
wording that was pinned for this session. Powers the Sessions journey panel.

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sessions/{session_id}/outcome`

Back-propagate the session outcome onto its precedent traces — this is what
makes the predictor prefer action paths that historically ended well.

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `EndSessionRequest`
| field | type | required | notes |
|---|---|---|---|
| `outcome` | any |  |  |

**Responses:** `200`

### `POST /sessions/{session_id}/plan-turn`

Plan Turn

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `PlanTurnRequest`
| field | type | required | notes |
|---|---|---|---|
| `user_message` | string | yes |  |
| `cohort` | string |  | (default ``) |
| `mood` | string |  | (default ``) |
| `state` | string |  | (default ``) |
| `action` | any |  |  |
| `prev_assistant_message` | any |  |  |

**Responses:** `200`

### `GET /sessions/{session_id}/pool`

Get Pool Snapshot

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sessions/{session_id}/realtime-token`

Realtime Token

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /sessions/{session_id}/voice-turn`

Classify + plan for one voice turn; the realtime model speaks the result.

**Parameters:** `session_id` (path, required), `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `VoiceTurnRequest`
| field | type | required | notes |
|---|---|---|---|
| `user_message` | string | yes |  |
| `prev_assistant_message` | any |  |  |

**Responses:** `200`


## Precedent traces

### `GET /traces`

List Traces

**Parameters:** `sop_id` (query), `action` (query), `outcome` (query), `session_id` (query), `q` (query), `limit` (query), `offset` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /traces/facets`

Distinct actions/outcomes for filter dropdowns (scoped to one SOP if given).

**Parameters:** `sop_id` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `GET /traces/summary`

Per-SOP totals + outcome mix — the browser's header strip.

**Parameters:** `X-Project` (header), `authorization` (header)

**Responses:** `200`


## A/B autopilot

### `GET /abtests`

List Abtests

**Parameters:** `sop_id` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`

### `POST /abtests`

Start Abtest

**Parameters:** `X-Project` (header), `authorization` (header)

**Request body** (`application/json`): `ABTestCreate`
| field | type | required | notes |
|---|---|---|---|
| `sop_id` | string | yes |  |
| `arm_a_version` | integer |  | (default `0`) |
| `arm_b_version` | integer |  | (default `0`) |
| `n_sessions` | integer |  | simulated sessions PER ARM (default `4`) |
| `max_turns` | integer |  | (default `8`) |
| `name` | string |  | (default ``) |

**Responses:** `200`

### `GET /abtests/{abtest_id}`

Get Abtest

**Parameters:** `abtest_id` (path, required), `X-Project` (header), `authorization` (header)

**Responses:** `200`


## Metrics

### `GET /metrics/summary`

Summary

**Parameters:** `days` (query), `X-Project` (header), `authorization` (header)

**Responses:** `200`
