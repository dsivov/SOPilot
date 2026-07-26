// Stage-0 System Analysis Report — the structured JSON an (offline) analysis
// produces to seed the Stage-1 schema. See docs/DESIGN_SYSTEM_ANALYSIS_REPORT.md.
// The configurator only ever consumes this report, never the raw system.
import type { Config } from "./configModel";
import type { FieldType } from "./configVocab";
import type { ConfigSchema, SchemaFieldDef, ToolDef, StructureDef, ItemStatus } from "./schema";

export const REPORT_KIND = "sopilot-system-analysis";

export interface Component {
  id: string;
  name?: string;
  kind?: string;                 // service | external | datastore | …
  description?: string;
  depends_on?: string[];
  exposes?: Array<{ kind: string; path?: string; methods?: string[] }>; // inbound endpoints (architectural)
}

export interface ConfigItem {
  path: string;
  item: "field" | "tool" | "structure";
  type?: FieldType;              // fields
  options?: string[];           // enum fields
  default?: unknown;
  required?: boolean;
  description?: string;
  advanced?: boolean;
  component?: string;
  status?: ItemStatus;          // known | needs_input | inferred
  source?: string;              // sample_config | db_schema | code | manual
}

export interface IntegrationPoint {
  name: string;
  kind?: string;                // db | http | webhook | mcp | rag
  direction?: "outbound" | "inbound";
  technology?: Record<string, unknown>;  // {dialect, driver, protocol, method, …}
  target?: Record<string, unknown>;      // {url_field, host_field, port, database, …}
  auth?: { secret_ref?: string; header?: string };
  description?: string;
  component?: string;
  status?: ItemStatus;
  source?: string;
}

export interface OpenQuestion {
  about?: string;               // a path or component id
  question: string;
  needs_from?: string;          // engineering | admin
}

export interface AnalysisReport {
  kind: string;
  report_version?: number;
  system?: { name?: string; description?: string; analyzed_at?: string; sources?: string[] };
  components?: Component[];
  config_items?: ConfigItem[];
  integration_points?: IntegrationPoint[];
  open_questions?: OpenQuestion[];
}

const _TYPES = new Set<FieldType>(["string", "number", "boolean", "enum", "secret-ref", "connector-ref"]);

// Shape-check an uploaded report. Returns an error string or null.
export function validateReport(r: any): string | null {
  if (!r || typeof r !== "object") return "not a JSON object";
  if (r.kind !== REPORT_KIND) return `not a ${REPORT_KIND} report (kind = ${JSON.stringify(r.kind)})`;
  if (!Array.isArray(r.config_items)) return "config_items must be a list";
  const seen = new Set<string>();
  for (let i = 0; i < r.config_items.length; i++) {
    const c = r.config_items[i];
    if (!c || typeof c !== "object") return `config_item ${i}: not an object`;
    if (!String(c.path || "").trim()) return `config_item ${i}: missing path`;
    if (!["field", "tool", "structure"].includes(c.item)) return `config_item '${c.path}': item must be field|tool|structure`;
    if (c.item === "field" && c.type && !_TYPES.has(c.type)) return `config_item '${c.path}': bad type '${c.type}'`;
    const key = `${c.item}:${c.path}`;
    if (seen.has(key)) return `config_item: duplicate ${key}`;
    seen.add(key);
  }
  return null;
}

// Report → ConfigSchema (the Stage-1 seed). Fields/tools/structures come from
// config_items; status (esp. needs_input) carries into field defs.
export function schemaFromAnalysisReport(r: AnalysisReport): ConfigSchema {
  const items = r.config_items ?? [];
  const fields: SchemaFieldDef[] = items.filter((c) => c.item === "field").map((c) => ({
    path: c.path,
    type: (c.type && _TYPES.has(c.type) ? c.type : "string") as FieldType,
    description: c.description,
    options: c.type === "enum" ? (c.options ?? []) : undefined,
    required: c.required,
    advanced: c.advanced,
    status: c.status,
  }));
  const tools: ToolDef[] = items.filter((c) => c.item === "tool").map((c) => ({
    name: c.path, description: c.description,
  }));
  const structures: StructureDef[] = items.filter((c) => c.item === "structure").map((c) => ({
    key: c.path, label: c.description,
  }));
  return { name: r.system?.name || "system", description: r.system?.description, fields, tools, structures };
}

// Items the report flagged as unresolved — surfaced to the admin after import.
export function needsInputPaths(r: AnalysisReport): string[] {
  return (r.config_items ?? []).filter((c) => c.status === "needs_input").map((c) => c.path);
}

export interface MergeSummary { added: string[]; updated: string[]; removed: string[] }

// Merge a (re-)analysis report into an EXISTING schema, preserving the admin's
// curation. Report is authoritative for existence / type / enum options /
// status; the admin keeps required / advanced / label and any non-empty
// description. Fields the report no longer has are KEPT but reported as removed
// (never silently dropped) so the admin decides.
export function mergeReportIntoSchema(
  schema: ConfigSchema, report: AnalysisReport,
): { schema: ConfigSchema; summary: MergeSummary } {
  const fresh = schemaFromAnalysisReport(report);
  const byPath = new Map(schema.fields.map((f) => [f.path, f]));
  const reportPaths = new Set(fresh.fields.map((f) => f.path));
  const added: string[] = [], updated: string[] = [];

  const mergedFields: SchemaFieldDef[] = fresh.fields.map((rf) => {
    const ex = byPath.get(rf.path);
    if (!ex) { added.push(rf.path); return rf; }
    updated.push(rf.path);
    return {
      ...rf,                                   // report: type, options, status
      required: ex.required ?? rf.required,    // admin overrides preserved
      advanced: ex.advanced ?? rf.advanced,
      label: ex.label ?? rf.label,
      description: ex.description || rf.description,
    };
  });
  // keep admin fields the report dropped (flag, don't lose)
  const removed = schema.fields.filter((f) => !reportPaths.has(f.path)).map((f) => f.path);
  for (const f of schema.fields) if (!reportPaths.has(f.path)) mergedFields.push(f);

  // tools/structures: union by name/key, keeping admin descriptions
  const toolByName = new Map((schema.tools ?? []).map((t) => [t.name, t]));
  const tools: ToolDef[] = (fresh.tools ?? []).map((t) => ({ ...t, description: toolByName.get(t.name)?.description || t.description }));
  for (const t of schema.tools ?? []) if (!tools.some((x) => x.name === t.name)) tools.push(t);
  const structKeys = new Set((fresh.structures ?? []).map((s) => s.key));
  const structures: StructureDef[] = [...(fresh.structures ?? [])];
  for (const s of schema.structures ?? []) if (!structKeys.has(s.key)) structures.push(s);

  return {
    schema: { ...schema, name: schema.name || fresh.name, fields: mergedFields, tools, structures },
    summary: { added, updated, removed },
  };
}

// Render the report's architecture (components + dependencies + integration
// points) as a dependency Graph for ConfigGraph. Columns: services → datastores
// → external/integration points.
export function reportToGraph(r: AnalysisReport): { nodes: any[]; edges: any[] } {
  const nodes: any[] = [], edges: any[] = [];
  const seen = new Set<string>();
  const colFor = (kind?: string) => (kind === "service" ? 0 : kind === "datastore" ? 1 : 2);
  const gkindFor = (kind?: string) => (kind === "service" ? "agent" : kind === "datastore" ? "backend" : "mcp");
  for (const c of r.components ?? []) {
    if (!c.id || seen.has(c.id)) continue;
    seen.add(c.id);
    const exposeN = (c.exposes ?? []).length;
    nodes.push({ id: c.id, label: c.name || c.id, kind: gkindFor(c.kind), col: colFor(c.kind), status: "info",
      sub: [c.kind, exposeN ? `${exposeN} endpoint${exposeN === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ") });
  }
  for (const c of r.components ?? []) {
    for (const dep of c.depends_on ?? []) {
      if (seen.has(dep)) edges.push({ from: c.id, to: dep, status: "info" });
    }
  }
  for (const ip of r.integration_points ?? []) {
    const id = `ip:${ip.name}`;
    if (!seen.has(id)) {
      seen.add(id);
      const tech = ip.technology ? Object.values(ip.technology).filter((v) => typeof v === "string").join("/") : "";
      nodes.push({ id, label: ip.name, kind: ip.kind === "db" ? "backend" : "mcp", col: 2,
        status: ip.status === "needs_input" ? "warn" : "info", sub: [ip.kind, tech].filter(Boolean).join(" · ") });
    }
    if (ip.component && seen.has(ip.component)) edges.push({ from: ip.component, to: id, status: "info" });
  }
  return { nodes, edges };
}

// A tiny bootstrap "report" derived from a loaded config, so the import flow can
// be demonstrated without a full analysis run. (Real reports come from Stage 0.)
export function reportFromConfig(cfg: Config): AnalysisReport {
  const items: ConfigItem[] = [];
  for (const [k, v] of Object.entries(cfg.tools ?? {})) {
    void v; items.push({ path: k, item: "tool", status: "known", source: "sample_config" });
  }
  return {
    kind: REPORT_KIND, report_version: 1,
    system: { name: String(cfg.display_name || "system"), sources: ["sample_config"] },
    config_items: items,
  };
}
