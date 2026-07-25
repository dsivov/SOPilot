# Stage-1 Config Schema (the "available options" DSL) — design

Status: **proposal** (not built). Branch: `feature/config-management`.
Author context: config-management feature, extends the two-stage thesis.

## 1. The gap

The config-management feature is two LLM-assisted stages over a formal engine:

- **Stage 1 (admin/engineer)** — defines the *bounds*.
- **Stage 2 (user)** — is guided *within* them.

What Stage 1 authors today is only the **constraint layer**: `enum / requires /
conflicts` **rules** (`frontend/src/config/rules.ts`), LLM-drafted
(`/config/draft-rule`), versioned and published (`ConfigRuleset`), enforced by
Stage 2.

What Stage 1 **cannot** author today is the **schema itself** — *which*
tools/fields/structures exist, their types, allowed values, defaults,
requiredness, and descriptions. That vocabulary is **discovered** from a loaded
example config (`configVocab.deriveFields`), not **declared**. Consequences:

- You can only constrain fields that already appear in a sample — you can't
  define the config surface for a **new product** from scratch.
- The vocabulary is AENA-shaped by accident of the example, not authoritative.
- The assistant and widgets have no field **descriptions**, types beyond a
  cheap guess, or notion of "this option exists but is unset."

The user named this precisely: *"stage 1 — admin/engineer defines the DSL /
available config options."* This document designs that.

## 2. Concept: schema = the DSL, rules = the grammar on top

Clean separation of the two Stage-1 artifacts:

| Artifact | Declares | Example |
|---|---|---|
| **Config Schema** (new) | *what exists* and its **type-level** shape | `voice` is an enum of {alloy,echo,…}; `max_call_duration` is a number; `notification_service_url` is a string, required |
| **Rules** (exists) | **cross-field / relational** logic the type system can't express | if `tool:send_email` then `field:notification_service_url` must be set; `A conflicts with B` |

The schema is the **DSL** the admin defines; rules are relational constraints on
top; Stage 2 is bounded by **both**. Enum-ness moves into the schema (a declared
enum field *is* the constraint) — the standalone `enum` **rule** kind is
subsumed by schema enum fields (migration in §7). `requires` / `conflicts`
remain rules.

## 3. The schema model

```ts
// frontend/src/config/schema.ts (new)
type FieldType = "string" | "number" | "boolean" | "enum" | "secret-ref" | "connector-ref";

interface FieldDef {
  path: string;            // dot-path, e.g. "custom_config.gpt_model"
  type: FieldType;
  label?: string;
  description?: string;    // shown as help in Stage 2 (ties into the "chat/get-help" work)
  options?: string[];      // enum values (type === "enum")
  default?: unknown;
  required?: boolean;      // engine treats as an always-active requires
  group?: string;          // organizes the Stage-2 field panel (replaces the ad-hoc `advanced` flag)
  advanced?: boolean;      // hidden by default (kept for infra plumbing)
}

interface ToolDef {        // which built-in tools MAY be enabled, described
  name: string;
  label?: string;
  description?: string;
  category?: string;
}

interface StructureDef {   // shape of a declared list (mcp_servers, knowledge_base, …)
  key: string;             // "mcp_servers"
  itemFields: FieldDef[];  // shape of one entry
  label?: string;
}

interface ConfigSchema {
  name: string;            // e.g. "polartie-voice-robot"
  description?: string;
  strict?: boolean;        // closed-world? (see §6) — default false
  fields: FieldDef[];
  tools: ToolDef[];
  structures: StructureDef[];
}
```

Notes:
- `secret-ref` / `connector-ref` types tie into the connector-registry work
  (`§ write-back`): a field typed `secret-ref` renders as a secret picker and
  resolves via `/config/render-robot`.
- `group` supersedes the current binary `advanced` flag with real sections;
  `advanced` stays as a per-field "hide by default" hint.

## 4. Persistence — extend the existing ruleset, don't add a parallel store

Today: `ConfigRuleset` / `ConfigRulesetVersion` (SopVersion-style; one "default"
per project; `published_version`). The version payload column is `rules: JSON`.

Proposal — **one profile, published as a unit** (schema + rules evolve
together and Stage 2 needs both):

- Add a **nullable** `schema: JSON` column to `ConfigRulesetVersion` (additive
  migration; existing rows → `schema = null`).
- `GET /config/ruleset` also returns `schema` / `published_schema`.
- `PUT /config/ruleset` accepts `{rules, schema, publish}` (both optional;
  either can be authored independently, published together).
- Conceptually rename to "config profile" in UI copy; keep table names.

Back-compat: a project with no published schema (`schema = null`) → Stage 2
falls back to `deriveFields` exactly as today. Nothing breaks on day one.

> **Decided (next stage):** the **config documents themselves must be stored in
> the database, versioned** — like SOPs and the ruleset (SopVersion-style:
> immutable versions, a published pointer, per tenant/project). The current
> localStorage persistence (`sopilot-config:<project>`) is an interim
> working-copy only; it is replaced by DB-backed `ConfigDocument` /
> `ConfigDocumentVersion` with GET/PUT/publish and a history view. This lands in
> the **next stage**, alongside — and naturally paired with — the schema work
> here (the schema types the config; the versioned document is the config).
> Export / "Download robot config" becomes a render of a stored version, not of
> ephemeral editor state.

## 5. `deriveFields` is demoted to a bootstrap importer

`configVocab.deriveFields(cfg)` stops being the runtime vocabulary source and
becomes **"seed a schema from an existing config"** — a one-click importer in
the Stage-1 UI: walk a loaded config → draft `FieldDef[]` (type + advanced
already inferred) + `ToolDef[]` (from `cfg.tools`) + structure shapes. The
admin then curates (labels, descriptions, enums, required, groups) and
publishes. This is how an existing robot config becomes an authored schema in
seconds, and how AENA migrates.

Runtime precedence, everywhere vocabulary is consumed:

```
published schema  ?  →  use it (authoritative)
                  :  →  deriveFields(loadedConfig)   // bootstrap fallback
```

Touch points (all already parameterized, minimal change):
- `rules.ts::ruleVocabulary` → from schema when present.
- `ConfigEdit.tsx` field panel → schema FieldDefs (type/options/description/
  group drive widgets + help; required flagged).
- `/config/draft-edit` and `/config/draft-rule` → grounded in the schema
  (authoritative field list, types, descriptions) — better proposals, and the
  assistant can *explain options* (the "how do I…" answers get richer).
- `/config/render-robot` → **validate** against the schema (types, enum
  membership, required present) before emitting the artifact.

## 6. Open-world vs strict (a decision to confirm)

A real robot config carries infra keys we don't want to force-declare
(`rem_ws_host`, VAD thresholds, …). So:

- **Default: open-world.** The schema *describes and types* known options;
  undeclared keys pass through untouched (today's behavior). Best for
  onboarding an existing product.
- **Opt-in `strict: true`: closed-world.** Only declared fields/tools may
  appear; the guided editor won't surface undeclared keys and write-back flags
  them. For locked-down products where the admin owns the entire surface.

Recommendation: ship open-world first; `strict` in a later phase.

## 7. Migration of the existing `enum` rule kind

Enum constraints exist today as a `Rule` kind. Once schema enum fields exist,
they're redundant. Plan:
- On schema publish, auto-convert any `enum` rules whose `field` matches a
  declared field into that field's `options` (and drop the rule).
- The engine keeps understanding `enum` rules for unschematized projects, so
  nothing breaks mid-migration.
- New authoring: enum-ness is a **field property**, not a rule.

## 8. Stage-1 authoring UX (Config admin view)

The Config admin view gains a **Schema** section beside the existing Rules
section (same view — they're one profile):

- **Bootstrap from config** button → `deriveFields` seed (§5).
- **Fields table**: add/edit `FieldDef` rows (path, type dropdown, enum options,
  default, required, group, description, advanced).
- **Tools**: declare available tools with descriptions (seeded from
  `cfg.tools`), toggle which are offerable.
- **Structures**: item-shape editors for the known lists (phase 2 for custom).
- **LLM-assisted schema authoring** (parallel to `draft-rule`): a new
  `/config/draft-field` — *"add a weather_provider field, enum of
  openweather/tomorrow.io, describe it"* → drafts one `FieldDef`. This extends
  the "chat with the system" thesis to **schema definition**, closing the loop
  the user asked about: chat to define the DSL, chat to author rules, chat to
  edit within them.
- **Save draft / Save & publish** — publishes schema + rules together.

## 9. Phasing

- **Phase 1 (foundation):** schema model + `schema` column + `GET/PUT
  /config/ruleset` carrying it + `deriveFields` bootstrap importer + Stage-2
  **field** vocabulary/widgets from schema (fallback preserved). No behavior
  change until a schema is published.
- **Phase 2 (authoring + reach):** LLM `draft-field`, tools & structure
  declaration, write-back schema validation, enum-rule migration, field
  descriptions surfaced as Stage-2 help.
- **Phase 3 (lock-down + extensibility):** `strict` closed-world, custom
  structures, `secret-ref`/`connector-ref` widgets wired to the registry.

**Paired next-stage work (decided): DB-versioned config documents.** Move the
config document itself from localStorage into the database, versioned
(`ConfigDocument` / `ConfigDocumentVersion`, SopVersion-style — immutable
versions, published pointer, per tenant/project; GET/PUT/publish + history).
The schema (this doc) types the config; the versioned document *is* the config;
the ruleset bounds it — three versioned, DB-backed artifacts per project.
Write-back renders a stored version. Sequence: this can land just before or
together with Phase 1, since the schema authoring and the document store share
the same persistence pattern already proven by `ConfigRuleset`.

## 10. Decisions to confirm before building

1. **One profile vs two stores** — recommend extending `ConfigRuleset` with a
   `schema` column (single publish). Alternative: a sibling `ConfigSchema`
   table.
2. **Required-ness** — recommend schema `required: true` (engine treats as an
   always-active requires) rather than a conditionless `requires` rule.
3. **Open-world default, `strict` opt-in** (§6) — recommend yes.
4. **Enum lives in schema** (subsumes the `enum` rule kind, §7) — recommend yes.
5. **Scope of Phase 1** — fields only, or fields + tools together? Recommend
   fields first (biggest payoff, smallest surface), tools in Phase 2.
