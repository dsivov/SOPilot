// Vocabulary derived from a LOADED config, not a hardcoded list — so the admin
// (rule authoring) and the user (guided editing), plus both LLM assistants, work
// against the atoms the actual document has. A different robot's fields show up
// automatically; nothing is AENA-shaped here.
import type { Config } from "./configModel";

export type FieldType = "string" | "number" | "boolean" | "enum";

export interface DerivedField {
  path: string;        // dot-path, e.g. "custom_config.gpt_model"
  type: FieldType;
  value: any;          // current value (undefined if unset)
  advanced: boolean;   // plumbing the admin usually shouldn't rule on — hidden by default
}

// Keys that are transport/infra plumbing rather than agent behaviour: still
// reachable (advanced toggle), just not shown by default. Matched on the LEAF
// key or the full dot-path.
const ADVANCED_LEAVES = new Set([
  "account_id", "type", "rem_ws_host", "recording_s3_presigned_url_generator_url",
  "pre_vad_enabled", "pre_vad_intensity_threshold", "pre_vad_density_threshold",
  "mcp_beep_enabled", "record_audio", "record_transcript", "record_summary",
  "record_camera_snapshots", "max_call_duration", "call_wrap_up_after",
]);

// Free-text / structural keys handled by dedicated editors or too large to be a
// scalar field — excluded from the flat scalar walk.
const SKIP_LEAVES = new Set([
  "prompt", "tools", "knowledge_base", "transfer_topics", "mcp_servers",
  "events", "visual_hints", "remote_view_config",
]);

function classify(v: any): FieldType | null {
  if (typeof v === "string") return "string";
  if (typeof v === "number") return "number";
  if (typeof v === "boolean") return "boolean";
  return null; // objects recursed into; arrays/null skipped as scalar fields
}

// Walk the config to scalar leaves (recursing into plain objects), returning one
// DerivedField per editable/constrainable scalar path.
export function deriveFields(cfg: Config): DerivedField[] {
  const out: DerivedField[] = [];
  const walk = (obj: any, prefix: string) => {
    for (const [k, v] of Object.entries(obj ?? {})) {
      if (SKIP_LEAVES.has(k)) continue;
      const path = prefix ? `${prefix}.${k}` : k;
      const t = classify(v);
      if (t) {
        out.push({ path, type: t, value: v, advanced: ADVANCED_LEAVES.has(k) || ADVANCED_LEAVES.has(path) });
      } else if (v && typeof v === "object" && !Array.isArray(v)) {
        walk(v, path); // nested object (e.g. custom_config, lightrag.postgres)
      }
      // arrays & null: not scalar fields (structures have their own editors)
    }
  };
  walk(cfg, "");
  // Fields a rule commonly needs that may be UNSET (absent from the walk) — surface
  // them so the admin/user can require or set them even before they exist.
  for (const p of ["notification_service_url", "opensearch_endpoint", "lightrag.postgres.host"]) {
    if (!out.some((f) => f.path === p)) out.push({ path: p, type: "string", value: undefined, advanced: false });
  }
  return out.sort((a, b) => (a.advanced === b.advanced ? a.path.localeCompare(b.path) : a.advanced ? 1 : -1));
}

// The index_modes actually present in the loaded config's knowledge bases
// (dynamic — no longer assuming simple|lightrag).
export function kbModesOf(cfg: Config): string[] {
  const modes = (cfg.knowledge_base ?? []).map((k: any): string => String(k.index_mode ?? "simple"));
  return [...new Set<string>(modes)];
}
