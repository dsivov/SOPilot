# Stage-0 System Analysis — playbook

**Goal:** given a target system's codebase, produce a
`sopilot-system-analysis` JSON report that seeds the Stage-1 config schema.
This is a Claude-driven discovery run (expert + Claude), not a deterministic
script — the steps below are the procedure a fresh Claude Code session follows.

Contract & rationale: [`DESIGN_SYSTEM_ANALYSIS_REPORT.md`](DESIGN_SYSTEM_ANALYSIS_REPORT.md)
(and its `.html`). Worked examples:
[`examples/context-graph-analysis.json`](examples/context-graph-analysis.json),
[`examples/analysis-report.sample.json`](examples/analysis-report.sample.json).

## When to run
- Onboarding a new system, and
- after each major update of an already-onboarded system (re-analysis merges
  into the existing schema, preserving the admin's curation).

## Procedure

**1. Orient.** Read the top-level docs and manifests:
`README*`, `CLAUDE.md`/`AGENTS.md`, `docker-compose*.yml`, `Dockerfile*`,
`pyproject.toml`/`package.json`, `k8s`/deploy manifests.

**2. Find the config surface** (these become `config_items`):
- env templates: `.env.example` / `env.example` / `config.ini.example`
- settings modules: `config.py` / `settings.py` / `Settings(BaseSettings)`
- feature flags, model/provider selectors, storage-backend selectors.
For each knob capture: dot-path (env var name or nested key), **type**
(`string|number|boolean|enum|secret-ref|connector-ref`), **enum options** if a
fixed set, `required`, one-line `description`, `advanced` (infra/plumbing hidden
by default), and `source` (`env.example|config.ini|code|db_schema|manual`).
Credentials are **`secret-ref`** — never capture the secret value.

**3. Map the architecture** (`components`):
services, datastores, external providers — each with `id`, `name`, `kind`
(`service|external|datastore`), `description`, `depends_on`. For services that
SERVE endpoints (FastAPI routers, an MCP mount, webhook receivers), list them
under `exposes` (kind/path/methods) — these are inbound, architectural, not
editable config.

**4. Map connections** (`integration_points`): every OUTBOUND system the app
talks to — databases and HTTP/webhook/MCP/RAG endpoints. Each carries:
`kind` (`db|http|webhook|mcp|rag`), `direction:"outbound"`, a `technology`
block (DB: `{dialect, driver, protocol}`; HTTP: `{protocol, method}`), a
`target` that references a config_item for the operator-set host/url
(`host_field`/`url_field`) plus `port`/`database`, and `auth: {secret_ref, header}`.
The connection string/credential is a `secret_ref`, not a stored value.

**5. Record the unknowns.** Anything analysis can't resolve (an enum whose
allowed values are deployment-specific, a provider not yet chosen): mark the
config_item `status:"needs_input"` (an enum with unknown values gets
`options: []`), and add an `open_questions` entry (`about`, `question`,
`needs_from: engineering|admin`).

**6. Emit** the report as JSON (shape below), then **validate**:

```bash
python scripts/validate_analysis_report.py <report.json>
```

**7. Load** it into SOPilot:
- **UI:** Config admin → *Available options — schema (DSL)* → **Import analysis
  report…** (seeds the schema on first import; merges on re-analysis). It is
  persisted DB-versioned via `/config/analysis`.
- **API:** `PUT /config/analysis {"report": <json>, "publish": true}` (stores
  the versioned report), then the admin curates + publishes the schema.

## Report shape (minimum)

```jsonc
{
  "kind": "sopilot-system-analysis",
  "report_version": 1,
  "system": { "name": "…", "description": "…", "sources": ["env.example","code"] },
  "components": [
    { "id": "api", "name": "API", "kind": "service", "depends_on": ["db"],
      "exposes": [ { "kind": "http-route", "path": "/query", "methods": ["POST"] } ] }
  ],
  "config_items": [
    { "path": "LLM_MODEL", "item": "field", "type": "string", "required": true,
      "description": "…", "component": "api", "status": "known", "source": "env.example" },
    { "path": "LLM_API_KEY", "item": "field", "type": "secret-ref", "description": "…", "status": "known" },
    { "path": "STORAGE", "item": "field", "type": "enum", "options": ["a","b"], "status": "known" }
  ],
  "integration_points": [
    { "name": "db", "kind": "db", "direction": "outbound",
      "technology": { "dialect": "postgresql", "driver": "asyncpg" },
      "target": { "host_field": "DB_HOST", "port": 5432 },
      "auth": { "secret_ref": "db_dsn" }, "status": "known", "source": "code" }
  ],
  "open_questions": [
    { "about": "STORAGE", "question": "which backend for this deployment?", "needs_from": "admin" }
  ]
}
```

- `config_items[].item`: `field | tool | structure` → maps 1:1 to the schema's
  fields / tools / structures.
- enum fields need `options` **unless** `status: "needs_input"`.
- keep `description` short and user-facing; the config manager shows it as help.

## Guardrails
- The report DESCRIBES; it holds no secret values (`secret-ref` only).
- Prefer real dot-paths from the system's own config; don't invent names.
- Be honest about `needs_input` — a flagged unknown is better than a guess.
- Keep it reviewable: dozens of items, not hundreds; group plumbing as
  `advanced: true`.
