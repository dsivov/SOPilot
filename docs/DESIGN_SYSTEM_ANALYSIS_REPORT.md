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
  "components": [
    { "id": "voice_agent", "name": "Voice Agent", "kind": "service",
      "description": "...", "depends_on": ["knowledge_mcp", "schedule_mcp"] },
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

  // Retrieval / external systems → connectors + structure entries.
  "integration_points": [
    { "name": "airport-facts", "kind": "rag",
      "source_ref": "postgres:aena.documents",   // db-as-source example
      "auth": "secret-ref", "description": "...", "component": "knowledge_mcp" }
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
