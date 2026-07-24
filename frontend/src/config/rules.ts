// Stage-1 (admin) constraint rules — the "rules become content, not code" layer.
//
// The Config viewer's checks live hard-coded in configModel.ts. This lifts the
// same class of check into a data-driven rule set an admin authors: the formal,
// feature-model half the design doc decided on — enum / requires / conflicts,
// evaluated by the same collect-findings→gate pattern. The user stage (Config.tsx)
// then enforces whatever the admin authored here, against the real config.
import { enabledTools, type Config, type Finding } from "./configModel";
import { deriveFields, kbModesOf } from "./configVocab";

export type Level = "error" | "warn";

// A predicate is a tiny atom over a config. Three forms (the whole vocabulary):
//   tool:send_email          a built-in tool is enabled (use | for any-of: tool:send_email|send_sms)
//   field:lightrag.postgres.host   a config field (dot path) is set / non-empty
//   kb_mode:lightrag         some knowledge_base entry uses this index_mode
export type Predicate = string;

export type Rule =
  | { id: string; kind: "requires"; when: Predicate; needs: Predicate; level: Level; msg: string }
  | { id: string; kind: "conflicts"; a: Predicate; b: Predicate; level: Level; msg: string }
  | { id: string; kind: "enum"; field: string; options: string[]; level: Level; msg: string };

export interface RuleResult { rule: Rule; level: Finding["level"]; msg: string; state: "violated" | "satisfied" | "inactive" }

// ---- predicate evaluation ---------------------------------------------------

function fieldValue(cfg: Config, path: string): any {
  return path.split(".").reduce<any>((o, k) => (o == null ? undefined : o[k]), cfg);
}
function isSet(v: any): boolean {
  if (v == null) return false;
  if (typeof v === "string") return v.trim() !== "";
  if (Array.isArray(v)) return v.length > 0;
  return Boolean(v);
}
function kbModes(cfg: Config): Set<string> {
  return new Set((cfg.knowledge_base ?? []).map((k: any) => k.index_mode ?? "simple"));
}

export function evalPredicate(cfg: Config, pred: Predicate): boolean {
  const [head, ...rest] = pred.split(":");
  const arg = rest.join(":");
  if (head === "tool") return arg.split("|").some((t) => enabledTools(cfg).includes(t.trim()));
  if (head === "field") return isSet(fieldValue(cfg, arg));
  if (head === "kb_mode") return kbModes(cfg).has(arg);
  return false;
}

export function describePredicate(pred: Predicate): string {
  const [head, ...rest] = pred.split(":");
  const arg = rest.join(":");
  if (head === "tool") return arg.includes("|") ? `one of [${arg.split("|").join(", ")}] enabled` : `${arg} enabled`;
  if (head === "field") return `${arg} is set`;
  if (head === "kb_mode") return `a knowledge base uses ${arg} mode`;
  return pred;
}

export function describeRule(r: Rule): string {
  if (r.kind === "requires") return `if ${describePredicate(r.when)} → require ${describePredicate(r.needs)}`;
  if (r.kind === "conflicts") return `${describePredicate(r.a)} conflicts with ${describePredicate(r.b)}`;
  return `${r.field} must be one of [${r.options.join(", ")}]`;
}

// ---- the engine -------------------------------------------------------------

export function evaluateRules(cfg: Config, rules: Rule[]): RuleResult[] {
  return rules.map((r): RuleResult => {
    if (r.kind === "requires") {
      if (!evalPredicate(cfg, r.when)) return { rule: r, level: "info", msg: describeRule(r), state: "inactive" };
      return evalPredicate(cfg, r.needs)
        ? { rule: r, level: "ok", msg: `${describePredicate(r.when)} — ${describePredicate(r.needs)} ✓`, state: "satisfied" }
        : { rule: r, level: r.level, msg: r.msg, state: "violated" };
    }
    if (r.kind === "conflicts") {
      return evalPredicate(cfg, r.a) && evalPredicate(cfg, r.b)
        ? { rule: r, level: r.level, msg: r.msg, state: "violated" }
        : { rule: r, level: "info", msg: describeRule(r), state: "inactive" };
    }
    // enum
    const v = fieldValue(cfg, r.field);
    if (!isSet(v)) return { rule: r, level: "info", msg: describeRule(r), state: "inactive" };
    return r.options.includes(String(v))
      ? { rule: r, level: "ok", msg: `${r.field} = "${v}" ✓`, state: "satisfied" }
      : { rule: r, level: r.level, msg: `${r.msg} (got "${v}")`, state: "violated" };
  });
}

// Just the problems, for the user stage's lint gate (mirrors validateConfig's shape).
export function ruleFindings(cfg: Config, rules: Rule[]): Finding[] {
  return evaluateRules(cfg, rules)
    .filter((r) => r.state === "violated")
    .map((r) => ({ level: r.level, msg: r.msg }));
}

// ---- the seed ruleset -------------------------------------------------------
// The hard-coded checks in configModel.ts / validateConfig, re-expressed as data
// so an admin can read, edit, and add to them. Same PolarTie foot-guns, now content.
export function seedRules(): Rule[] {
  return [
    {
      id: "email-needs-notif", kind: "requires",
      when: "tool:send_email|send_email_no_verify|send_verification_email|send_sms|send_sms_no_verify|send_verification_sms",
      needs: "field:notification_service_url", level: "error",
      msg: "An email/SMS tool is enabled but notification_service_url is empty — it will silently fail to load.",
    },
    {
      id: "lightrag-needs-postgres", kind: "requires",
      when: "kb_mode:lightrag", needs: "field:lightrag.postgres.host", level: "error",
      msg: "A LightRAG knowledge base is configured but lightrag.postgres is missing — the KB won't resolve.",
    },
    {
      id: "simple-kb-needs-opensearch", kind: "requires",
      when: "kb_mode:simple", needs: "field:opensearch_endpoint", level: "error",
      msg: "A simple knowledge base is configured but opensearch_endpoint is missing — the KB won't resolve.",
    },
    {
      id: "transfer-needs-topics", kind: "requires",
      when: "tool:transfer", needs: "field:transfer_topics", level: "warn",
      msg: "transfer is enabled but no transfer_topics are defined — the agent has nowhere to transfer.",
    },
    {
      id: "one-kb-query-backend", kind: "conflicts",
      a: "tool:knowledge_base_query", b: "tool:knowledge_base_query_lightrag", level: "warn",
      msg: "Both the OpenSearch and LightRAG knowledge-query tools are enabled — pick one backend; both won't resolve cleanly.",
    },
    {
      id: "voice-enum", kind: "enum",
      field: "voice", options: ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"], level: "error",
      msg: "voice must be a supported OpenAI realtime voice",
    },
  ];
}

// Vocabulary offered to the admin (and to the LLM drafter) so predicates
// reference real atoms of the config rather than invented ones. Fields are
// DERIVED from the loaded config (see configVocab), not hardcoded — plus the
// structural atoms (arrays with their own editors) a rule may reference.
export function ruleVocabulary(cfg: Config): { tools: string[]; fields: string[]; kbModes: string[] } {
  const tools = Object.keys((cfg.tools ?? {}) as Record<string, any>).sort();
  const derived = deriveFields(cfg).map((f) => f.path);
  const structural = ["transfer_topics", "knowledge_base", "mcp_servers"];
  const fields = [...derived, ...structural.filter((s) => !derived.includes(s))];
  return { tools, fields, kbModes: kbModesOf(cfg) };
}
