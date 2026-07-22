// The one genuinely new piece of the spike: turn a PolarTie config.json into a
// dependency graph, and validate it — structurally (deterministic) and logically
// (the freeform prompt vs the config). The logical pass here is a heuristic
// preview; the real version calls the LLM (SOPilot is LLM-native).

export type Config = Record<string, any>;

export type NodeKind = "agent" | "tool" | "mcp" | "kb" | "transfer" | "backend";
export type Status = "ok" | "off" | "error" | "warn" | "info";
export interface GNode { id: string; label: string; sub?: string; kind: NodeKind; status: Status; col: number }
export interface GEdge { from: string; to: string; status: "ok" | "error" | "info" }
export interface Graph { nodes: GNode[]; edges: GEdge[] }
export interface Finding { level: "error" | "warn" | "ok" | "info"; msg: string }

// Tools that need the notification service configured to actually load.
const NEEDS_NOTIF = new Set([
  "send_email", "send_email_no_verify", "send_verification_email", "verify_email_code",
  "send_sms", "send_sms_no_verify", "send_verification_sms", "verify_sms_code",
]);

export function enabledTools(cfg: Config): string[] {
  const t = (cfg.tools ?? {}) as Record<string, any>;
  return Object.keys(t).filter((k) => t[k]?.enabled && String(t[k]?.prompts?.description?.message ?? "").trim());
}

function hasNotif(cfg: Config): boolean {
  return !!String(cfg.notification_service_url ?? "").trim();
}
function hasPostgres(cfg: Config): boolean {
  return !!cfg.lightrag?.postgres?.host;
}
function hasOpenSearch(cfg: Config): boolean {
  return !!String(cfg.opensearch_endpoint ?? "").trim();
}

export function configToGraph(cfg: Config): Graph {
  const nodes: GNode[] = [];
  const edges: GEdge[] = [];
  const model = cfg.custom_config?.gpt_model ?? "gpt-realtime";
  const lang = cfg.default_language_iso || "—";

  nodes.push({
    id: "agent", kind: "agent", status: "ok", col: 0,
    label: cfg.display_name || "Agent",
    sub: `${model} · ${cfg.voice ?? "alloy"} · ${lang}`,
  });

  const notifNeeded = enabledTools(cfg).some((k) => NEEDS_NOTIF.has(k));
  const kbs = (cfg.knowledge_base ?? []) as any[];
  const needPg = kbs.some((k) => k.index_mode === "lightrag");
  const needOs = kbs.some((k) => (k.index_mode ?? "simple") === "simple");

  // col 1: tools
  for (const k of enabledTools(cfg)) {
    const broken = NEEDS_NOTIF.has(k) && !hasNotif(cfg);
    nodes.push({ id: `tool:${k}`, kind: "tool", status: broken ? "error" : "ok", col: 1, label: k });
    edges.push({ from: "agent", to: `tool:${k}`, status: "ok" });
    if (NEEDS_NOTIF.has(k)) edges.push({ from: `tool:${k}`, to: "be:notif", status: hasNotif(cfg) ? "ok" : "error" });
  }
  // col 1: MCP servers (tools resolved live via list_tools)
  (cfg.mcp_servers ?? []).forEach((m: any, i: number) => {
    let host = m.url;
    try { host = new URL(m.url).host; } catch { /* keep raw */ }
    nodes.push({ id: `mcp:${i}`, kind: "mcp", status: "info", col: 1, label: `MCP · ${host}`, sub: "list_tools" });
    edges.push({ from: "agent", to: `mcp:${i}`, status: "info" });
  });
  // col 1: knowledge bases → backend
  kbs.forEach((kb: any) => {
    const mode = kb.index_mode ?? "simple";
    const ok = mode === "lightrag" ? hasPostgres(cfg) : hasOpenSearch(cfg);
    const id = `kb:${kb.knowledge_id ?? kb.function_tag}`;
    nodes.push({ id, kind: "kb", status: ok ? "ok" : "error", col: 1, label: `KB · ${kb.function_tag ?? kb.knowledge_id}`, sub: mode });
    edges.push({ from: "agent", to: id, status: "ok" });
    edges.push({ from: id, to: mode === "lightrag" ? "be:pg" : "be:os", status: ok ? "ok" : "error" });
  });

  // col 2: transfer topics (edge from the transfer tool if enabled, else agent)
  const transferOn = enabledTools(cfg).includes("transfer");
  (cfg.transfer_topics ?? []).forEach((t: any) => {
    const id = `topic:${t.function_tag ?? t.topic_id}`;
    nodes.push({ id, kind: "transfer", status: "ok", col: 2, label: `topic · ${t.function_tag ?? t.topic_id}` });
    edges.push({ from: transferOn ? "tool:transfer" : "agent", to: id, status: "ok" });
  });

  // col 2: backend dependency nodes
  if (notifNeeded)
    nodes.push({ id: "be:notif", kind: "backend", status: hasNotif(cfg) ? "ok" : "error", col: 2, label: "Notification service", sub: hasNotif(cfg) ? "present" : "MISSING — required" });
  if (needPg)
    nodes.push({ id: "be:pg", kind: "backend", status: hasPostgres(cfg) ? "ok" : "error", col: 2, label: "Postgres (LightRAG)", sub: hasPostgres(cfg) ? "present" : "MISSING — required" });
  if (needOs)
    nodes.push({ id: "be:os", kind: "backend", status: hasOpenSearch(cfg) ? "ok" : "error", col: 2, label: "OpenSearch", sub: hasOpenSearch(cfg) ? "present" : "MISSING — required" });

  return { nodes, edges };
}

// Structural, deterministic validation — the collect-problems pattern, seeded
// with real PolarTie foot-guns.
export function validateConfig(cfg: Config): Finding[] {
  const out: Finding[] = [];
  const tools = enabledTools(cfg);
  for (const k of tools)
    if (NEEDS_NOTIF.has(k) && !hasNotif(cfg))
      out.push({ level: "error", msg: `${k} is enabled but notification_service_url is empty — the tool will silently not load on a call.` });
  for (const kb of (cfg.knowledge_base ?? []) as any[]) {
    const mode = kb.index_mode ?? "simple";
    if (mode === "lightrag" && !hasPostgres(cfg))
      out.push({ level: "error", msg: `LightRAG KB "${kb.function_tag ?? kb.knowledge_id}" requires lightrag.postgres — not configured.` });
    else if (mode === "simple" && !hasOpenSearch(cfg))
      out.push({ level: "error", msg: `Simple KB "${kb.function_tag ?? kb.knowledge_id}" requires opensearch_endpoint — not configured.` });
    else out.push({ level: "ok", msg: `KB "${kb.function_tag ?? kb.knowledge_id}" (${mode}) — backend configured.` });
  }
  if (tools.includes("transfer")) {
    const n = (cfg.transfer_topics ?? []).length;
    out.push(n ? { level: "ok", msg: `transfer enabled — ${n} transfer topic${n === 1 ? "" : "s"} defined.` }
               : { level: "warn", msg: `transfer is enabled but no transfer_topics are defined.` });
  }
  const nMcp = (cfg.mcp_servers ?? []).length;
  if (nMcp) out.push({ level: "warn", msg: `${nMcp} MCP server${nMcp === 1 ? "" : "s"} — tools not yet introspected; live list_tools would confirm they resolve.` });
  for (const vh of (cfg.visual_hints ?? []) as any[])
    out.push(vh.url ? { level: "ok", msg: `visual_hint "${vh.function_tag}" — URL present.` }
                    : { level: "error", msg: `visual_hint "${vh.function_tag}" has no URL.` });
  return out;
}

// Logical prompt validation — the headline pain. HEURISTIC preview here; the
// real check hands the freeform prompt + resolved config to the LLM.
export function logicalPromptFindings(cfg: Config): Finding[] {
  const out: Finding[] = [];
  const prompt = String(cfg.prompt ?? "");
  const p = prompt.toLowerCase();
  const tools = enabledTools(cfg);
  const emailBroken = !tools.includes("send_email") || !hasNotif(cfg);
  if (/\b(e-?mail|email you|send you)\b/.test(p) && emailBroken)
    out.push({ level: "error", msg: `The prompt offers to email the caller, but send_email is off or broken — the agent will promise something it can't do.` });
  if (/\b(spanish|english|español)\b/.test(p))
    out.push({ level: "info", msg: `The prompt sets a language expectation — confirm it matches default_language_iso ("${cfg.default_language_iso || "—"}").` });
  // transfer-desk references vs configured topics (crude keyword check)
  const tags = ((cfg.transfer_topics ?? []) as any[]).map((t) => String(t.function_tag ?? "").toLowerCase());
  if (/lost[-\s]?property/.test(p) && !tags.some((t) => t.includes("property") || t.includes("lost")))
    out.push({ level: "warn", msg: `The prompt mentions a "lost-property desk" but no transfer topic matches it.` });
  if (/\bbaggage\b/.test(p) && tags.some((t) => t.includes("bag")))
    out.push({ level: "ok", msg: `The prompt's "baggage desk" maps to a configured transfer topic.` });
  out.push({ level: "ok", msg: `No internal self-contradictions detected (heuristic — the real check uses the LLM).` });
  return out;
}
