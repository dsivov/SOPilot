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

export interface ToolDef {
  name: string;
  label?: string;
  description?: string;
  category?: string;
}

export interface StructureDef {
  key: string;             // "mcp_servers" | "knowledge_base" | "transfer_topics"
  label?: string;
}

export interface ConfigSchema {
  name?: string;
  description?: string;
  strict?: boolean;        // closed-world (Phase 3) — undeclared keys forbidden
  fields: SchemaFieldDef[];
  tools?: ToolDef[];       // which built-in tools MAY be enabled (allowlist + help)
  structures?: StructureDef[]; // which list structures are available
}

// The three list structures the editor knows how to render.
export const KNOWN_STRUCTURES: { key: string; label: string }[] = [
  { key: "mcp_servers", label: "MCP servers" },
  { key: "knowledge_base", label: "Knowledge bases" },
  { key: "transfer_topics", label: "Transfer topics" },
];

function getPath(cfg: any, path: string): any {
  return path.split(".").reduce<any>((o, k) => (o == null ? undefined : o[k]), cfg);
}

// Bootstrap importer: seed a schema from a loaded config so an existing robot
// config becomes an authored schema in one click (the admin then curates).
// Seeds fields, the tool allowlist (every tool the config has), and the
// structures present — all editable afterwards.
export function schemaFromConfig(cfg: Config): ConfigSchema {
  const toolNames = Object.keys((cfg.tools ?? {}) as Record<string, unknown>).sort();
  const present = KNOWN_STRUCTURES.filter((s) => Array.isArray((cfg as any)[s.key]));
  return {
    name: String(cfg.display_name || "config"),
    fields: deriveFields(cfg).map((f): SchemaFieldDef => ({ path: f.path, type: f.type, advanced: f.advanced })),
    tools: toolNames.map((name): ToolDef => ({ name })),
    structures: present.map((s): StructureDef => ({ key: s.key, label: s.label })),
  };
}

// enum RULES whose field is a declared schema field are redundant once the schema
// exists (enum lives in the schema). Fold them into the field's options and drop
// the rule — the design's Stage-1 migration, applied at save time.
export function foldEnumRulesIntoSchema<R extends { kind: string; field?: string; options?: string[] }>(
  rules: R[], schema: ConfigSchema | null,
): { rules: R[]; schema: ConfigSchema | null } {
  if (!schema?.fields?.length) return { rules, schema };
  const byPath = new Map(schema.fields.map((f) => [f.path, f]));
  const kept: R[] = [];
  let fields = schema.fields;
  let changed = false;
  for (const r of rules) {
    const f = r.kind === "enum" && r.field ? byPath.get(r.field) : undefined;
    if (f && r.options?.length) {
      fields = fields.map((x) => (x.path === f.path ? { ...x, type: "enum", options: r.options } : x));
      changed = true;
      continue; // drop the rule — now a schema enum field
    }
    kept.push(r);
  }
  return changed ? { rules: kept, schema: { ...schema, fields } } : { rules, schema };
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
