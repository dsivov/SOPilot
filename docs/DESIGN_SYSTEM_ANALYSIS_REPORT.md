# System Analysis Report → Stage-1 schema — design

Status: **proposal** (not built). Branch: `feature/config-management`.
Companion to [`DESIGN_CONFIG_SCHEMA_DSL.md`](DESIGN_CONFIG_SCHEMA_DSL.md).
Source feature: `NEW_FEATURES.md` — "Enhanced System Analysis".

## 1. Where this sits

The config-management pipeline, front to back:

```
Stage 0  ANALYSIS  → a structured JSON report of a target system
                     (components, all possible config items, integrations,
                      dependencies, and what's still unknown)
Stage 1  SCHEMA    → the admin curates a ConfigSchema (the DSL) seeded from
                     the report; declares rules (bounds)
Stage 2  CONFIG    → the user authors a config within schema + rules
         WRITE-BACK→ rendered, validated deploy artifact
```

Today Stage 1 is seeded from a **sample config** (`schemaFromConfig` →
`deriveFields`), which only sees fields that appear in one example. The
Analysis report replaces that weak seed with an **authoritative** one: it names
*all* config items with their types, enums, defaults, requiredness,
dependencies, and — crucially — the ones that are **not yet known** and need
Engineering input. This is Stage 0: the "Engineering Team" input the
NEW_FEATURES doc describes.

The configurator **only ever consumes the report**, never the raw system. The
report is the normalized output of analysis whose *input* may be a sample
config, a database schema, source code, or expert knowledge — a clean boundary
that keeps the configurator source-agnostic (the doc's point that "sometimes
the DB schema itself is the data source").

## 2. The report JSON (the keystone)

```jsonc
{
  "kind": "sopilot-system-analysis",
  "report_version": 1,
  "system": {
    "name": "polartie-voice-robot",
    "description": "...",
    "analyzed_at": "2026-07-25T…",           // stamped by the analysis run
    "sources": ["sample_config", "db_schema"] // what the analysis drew on
  },

  // Architecture — renders as a dependency diagram (reuses ConfigGraph).
  // `exposes` = INBOUND endpoints the component serves (FastAPI routes, the
  // /mcp mount, webhook receivers) — architectural, not editable config.
  "components": [
    { "id": "voice_agent", "name": "Voice Agent", "kind": "service",
      "description": "...", "depends_on": ["knowledge_mcp", "schedule_mcp"] },
    { "id": "sopilot_api", "name": "SOPilot API", "kind": "service",
      "exposes": [
        { "kind": "http-route", "path": "/config/ruleset", "methods": ["GET","PUT"] },
        { "kind": "mcp-mount", "path": "/mcp" },
        { "kind": "webhook-receiver", "path": "/hooks/twilio", "methods": ["POST"] }
      ] },
    { "id": "knowledge_mcp", "name": "Airport Knowledge MCP", "kind": "external",
      "description": "Context Graph", "depends_on": [] }
  ],

  // The config surface → becomes ConfigSchema fields / tools / structures.
  "config_items": [
    { "path": "voice", "item": "field", "type": "enum",
      "options": ["alloy", "echo", "coral"], "required": false,
      "description": "TTS voice", "component": "voice_agent",
      "status": "known", "source": "sample_config" },

    { "path": "weather_provider", "item": "field", "type": "enum",
      "options": [], "required": true, "description": "live weather source",
      "component": "voice_agent",
      "status": "needs_input", "source": "code" },          // options unknown

    { "path": "send_email", "item": "tool", "description": "send an email",
      "status": "known", "source": "code" },

    { "path": "mcp_servers", "item": "structure", "description": "MCP endpoints",
      "status": "known", "source": "sample_config" }
  ],

  // OUTBOUND connections the running agent makes (db / http / webhook / mcp /
  // rag). Non-secret SHAPE only — credentials/DSNs live in tenant_secrets and
  // are referenced by auth.secret_ref (never stored here). `technology` records
  // the connection tech; operator-settable host/url point at a config_item.
  "integration_points": [
    { "name": "aena_documents", "kind": "db", "direction": "outbound",
      "technology": { "dialect": "postgresql", "driver": "asyncpg", "protocol": "python" },
      "target": { "host_field": "lightrag.postgres.host", "port": 5432, "database": "rag" },
      "auth": { "secret_ref": "lightrag_dsn" },     // connection string/credential in tenant_secrets
      "component": "knowledge_mcp", "status": "known", "source": "db_schema" },

    { "name": "notify", "kind": "webhook", "direction": "outbound",
      "technology": { "protocol": "http", "method": "POST" },
      "target": { "url_field": "notification_service_url" },  // operator-set → a config_item
      "auth": { "secret_ref": "notify_token", "header": "Authorization" },
      "status": "known", "source": "code" },

    { "name": "airport-facts", "kind": "rag", "direction": "outbound",
      "technology": { "protocol": "http", "method": "POST" },
      "target": { "url_field": "opensearch_endpoint" },
      "auth": { "secret_ref": "opensearch_key" }, "component": "knowledge_mcp",
      "status": "known", "source": "sample_config" }
  ],

  // The feedback loop: what analysis could NOT resolve — surfaced to Engineering.
  "open_questions": [
    { "about": "weather_provider", "question": "what are the allowed providers?",
      "needs_from": "engineering" }
  ]
}
```

Notes:
- **`status: known | needs_input | inferred`** is the known-vs-unknown notion
  the NEW_FEATURES doc calls for. `needs_input` items appear in the schema but
  are flagged (Stage 2 can't finalize them; write-back warns) until Engineering
  supplies the missing facts.
- **`source`** per item records provenance (sample config / db schema / code /
  manual) — audit + merge decisions.
- **`config_items[].item`** (`field | tool | structure`) maps 1:1 to the three
  parts of `ConfigSchema`.
- `depends_on` on components (and optionally on config_items) drives the
  diagram and later cross-field rules.

### 2a · Connections, credentials, and web endpoints

The report separates three planes, and one hard rule governs secrets.

**The rule — secrets are never in the report or the config.** Connection
strings and credentials (a DB password, a DSN, an API token) live only in
`tenant_secrets` and are referenced by **`secret-ref`** (exactly as connector
auth works today). The report declares a secret is *needed*
(`auth.secret_ref`, `status: needs_input` until provisioned); it never carries
the value.

| Concern | Where it goes | Editable in Stage 2? |
|---|---|---|
| DB connection tech (driver/dialect/protocol) | `integration_points[].technology` | via a linked config_item, if any |
| DB/endpoint credential or DSN | **not stored** — `auth.secret_ref` → `tenant_secrets` | provisioned, not edited |
| Outbound webhook / HTTP / MCP / RAG | `integration_points` (kind + technology + target + auth + `direction:"outbound"`) | url/host via a config_item |
| Inbound FastAPI routes / `/mcp` / receivers | `components[].exposes` (architectural) | no (unless a knob → a config_item) |

- **Databases** are `integration_points` with `kind: "db"`. `technology`
  captures the "connection technology" — `dialect` (postgresql/mysql…),
  `driver` (asyncpg/psycopg/jdbc/odbc), `protocol` (python/jdbc). Multiple
  databases = multiple integration points, each with its own `secret_ref`.
  Non-secret, operator-settable parts (host, port, database) are plain values
  or a reference to a `config_item` (`host_field`) so they stay editable.
- A database that is only the **analysis producer's input** ("the DB schema is
  the data source") is *not* an integration point — it's recorded in
  `system.sources`. It becomes an integration point only if the *running* agent
  connects to it.
- **Web items split by direction.** OUTBOUND (the agent calls out — webhooks,
  notification URLs, HTTP/RAG endpoints, MCP servers) are `integration_points`
  with `direction: "outbound"`, a `technology` block, a `target` (url/host,
  usually a `config_item` reference so it's operator-set), and `auth.secret_ref`.
  INBOUND (endpoints the system *exposes* — FastAPI routers, the `/mcp` mount,
  webhook receivers) are architecture, listed under `components[].exposes`; the
  admin does not configure them unless a specific knob (bind port, path,
  enabled) is settable, which then becomes a `config_item`.

## 3. Importer: report → schema

A new importer beside `schemaFromConfig`:

```ts
schemaFromAnalysisReport(report): ConfigSchema
  fields:     config_items where item==="field"     (type, options, required, description, advanced)
  tools:      config_items where item==="tool"      (name=path, description)
  structures: config_items where item==="structure" (key=path)
```

`needs_input` items carry through as a field flag so the admin sees "declared
but awaiting Engineering." Everything the admin then edits (labels, extra
required flags, curated enum values) layers on top — same curate-then-publish
flow that exists now.

## 4. Re-analysis: versioned, DB-stored, merge-not-clobber

Per the decision that **all configs are DB-stored and versioned**, the report
is one of those artifacts. Analysis runs after each major system update or at
onboarding, so each run is a **new immutable version**:

- `ConfigAnalysisReport` / `ConfigAnalysisReportVersion` (SopVersion-style:
  immutable versions, a `published_version` pointer, per tenant/project),
  mirroring `ConfigRuleset`. `GET/PUT /config/analysis` + a history view.
- **Sync-to-schema is explicit and merges** — re-import must not wipe the
  admin's curation. Merge rule (by `path`):
  - the report is authoritative for **existence, type, dependencies, source,
    status** (structural facts);
  - the admin is authoritative for **label, description overrides, required,
    advanced, curated enum options** once they've touched a field;
  - **added** items appear flagged "new"; **removed** items are flagged
    "gone in vN" (not silently dropped) so the admin decides.
  Provenance (`source`, plus a "touched by admin" bit) is what lets the merge
  keep both honest.

Full artifact set per project, all versioned in the DB:
**analysis report** (Stage 0, describes) → **schema** (Stage 1, types) →
**ruleset** (bounds) → **config document** (the config itself) → rendered robot
config. The DB-versioning stage should stand all of these up on the one proven
`ConfigRuleset` pattern.

## 5. Diagram view

`components` + `depends_on` + `integration_points` render with the existing
`ConfigGraph` machinery — the "better in diagram form for human understanding"
ask, at low cost. Optional read-only view; the doc agrees the analysis itself
needs no authoring UI (it's an offline expert+Claude process producing the
JSON).

## 6. The feedback loop to Engineering

Two directions, both via `open_questions` / `needs_input`:
- **Analysis → Admin:** unresolved items arrive flagged; the admin can't
  finalize them.
- **Admin → Engineering:** when the admin needs an option the schema lacks
  (already surfaced today when the guided-edit assistant hits an out-of-vocab
  request), that becomes an `open_question` exported for the next analysis
  round. Closes the "if Stage 1 detects a missed feature, report it to
  Engineering" loop.

## 7. Open decisions

1. **Report JSON is the contract** — lock §2 first; the analysis producer
   (expert+Claude, offline) and the importer both bind to it.
2. **Scope of a report** — per project, or per *system* shared across projects
   that deploy it? (Lean: per system/tenant, referenced by projects — one AENA
   analysis, many project configs.)
3. **Merge policy** (§4) — the hard part; needs the "admin-touched" provenance
   bit designed in from the start.
4. **DB-as-source ingestion** — out of scope for the configurator (it consumes
   the report), but the *analysis producer* needs a convention for pointing at
   a DB/`source_ref`; keep it opaque to us for now.
5. **Sequencing** — this rides on the DB-versioning stage; do that first
   (config documents + this report + schema all get versioned tables together),
   then the importer + diagram + merge.

## 8. Phasing

- **Phase A:** lock the report JSON (§2); `schemaFromAnalysisReport` importer +
  "Import analysis report" in Config admin (file upload first — same shape as
  the config bootstrap); `needs_input` surfaced in schema + Stage 2.
- **Phase B:** DB-versioned `ConfigAnalysisReport`; sync-to-schema **merge**
  with provenance; history view.
- **Phase C:** diagram view; `open_questions` export (Admin→Engineering loop);
  in-UI help sourced from item descriptions (ties to NEW_FEATURES' help item).
