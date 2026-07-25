// Stage-1 config SCHEMA — the "available options" DSL the admin declares
// (see docs/DESIGN_CONFIG_SCHEMA_DSL.md). The schema says WHAT exists and its
// type-level shape; rules (rules.ts) stay the relational layer on top. Stage 2
// draws its field vocabulary from the PUBLISHED schema when present, and falls
// back to walking the loaded config (deriveFields) when a project has none.
import type { Config } from "./configModel";
import { deriveFields, type DerivedField, type FieldType } from "./configVocab";

export interface SchemaFieldDef {
  path: string;
  type: FieldType;
  label?: string;
  description?: string;
  options?: string[];      // enum values (type === "enum")
  default?: unknown;
  required?: boolean;
  group?: string;
  advanced?: boolean;
}

export interface ConfigSchema {
  name?: string;
  description?: string;
  strict?: boolean;        // closed-world (Phase 3) — undeclared keys forbidden
  fields: SchemaFieldDef[];
  tools?: unknown[];       // Phase 2
  structures?: unknown[];  // Phase 2
}

function getPath(cfg: any, path: string): any {
  return path.split(".").reduce<any>((o, k) => (o == null ? undefined : o[k]), cfg);
}

// Bootstrap importer: seed a schema from a loaded config so an existing robot
// config becomes an authored schema in one click (the admin then curates).
export function schemaFromConfig(cfg: Config): ConfigSchema {
  return {
    name: String(cfg.display_name || "config"),
    fields: deriveFields(cfg).map((f): SchemaFieldDef => ({ path: f.path, type: f.type, advanced: f.advanced })),
    tools: [],
    structures: [],
  };
}

// The fields Stage 2 edits: from the published schema if it declares any, else
// the config-derived fallback. Returns the shared DerivedField shape either way
// (schema fields carry value pulled from the current config + options/help).
export function editorFields(cfg: Config, schema: ConfigSchema | null | undefined): DerivedField[] {
  if (schema && Array.isArray(schema.fields) && schema.fields.length) {
    return schema.fields
      .map((f): DerivedField => ({
        path: f.path,
        type: f.type,
        value: getPath(cfg, f.path),
        advanced: !!f.advanced,
        options: f.type === "enum" ? f.options : undefined,
        description: f.description,
        required: f.required,
      }))
      .sort((a, b) => (a.advanced === b.advanced ? a.path.localeCompare(b.path) : a.advanced ? 1 : -1));
  }
  return deriveFields(cfg);
}

// Basis for the rule-authoring vocabulary and the LLM assistants when a schema
// is published: the declared field paths (else derived).
export function schemaFieldPaths(cfg: Config, schema: ConfigSchema | null | undefined): string[] {
  return editorFields(cfg, schema).map((f) => f.path);
}
