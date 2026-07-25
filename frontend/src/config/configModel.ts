// Turn a PolarTie config.json into a dependency graph, and validate it:
// structurally (deterministic), by MCP introspection (does the prompt reflect
// the tools the MCP servers actually provide — the prod-team pain), and
// logically (freeform prompt vs config; heuristic preview, real = LLM).

export type Config = Record<string, any>;
export type NodeKind = "agent" | "tool" | "mcp" | "kb" | "transfer" | "backend";
export type Status = "ok" | "off" | "error" | "warn" | "info";
export interface GNode { id: string; label: string; sub?: string; kind: NodeKind; status: Status; col: number }
export interface GEdge { from: string; to: string; status: "ok" | "error" | "info" }
export interface Graph { nodes: GNode[]; edges: GEdge[] }
export interface Finding { level: "error" | "warn" | "ok" | "info"; msg: string }
export type Introspection = Record<string, { tools: string[]; error?: string }>;

const NEEDS_NOTIF = new Set([
  "send_email", "send_email_no_verify", "send_verification_email", "verify_email_code",
  "send_sms", "send_sms_no_verify", "send_verification_sms", "verify_sms_code",
]);

// Built-in tools grouped into categories so a 26-tool config stays a readable graph.
const CATEGORY: Array<{ name: string; match: (t: string) => boolean }> = [
  { name: "Call control", match: (t) => ["hangup", "transfer"].includes(t) },
  { name: "Knowledge", match: (t) => t.startsWith("knowledge_base_query") || t.includes("knowledge_image") },
  { name: "Communication", match: (t) => /(email|sms)/.test(t) },
  { name: "Camera", match: (t) => t.includes("camera") },
  { name: "Input", match: (t) => /(drawing_pad|paintover|prompt_box)/.test(t) },
  { name: "Display", match: (t) => /^(show_|hide_|visual_hint|video_hint)/.test(t) || t === "show_mcp_image" },
  { name: "Behavior", match: (t) => ["stay_silent", "save_conversation_summary"].includes(t) },
];
function categorize(t: string): string { return CATEGORY.find((c) => c.match(t))?.name ?? "Other"; }

export function enabledTools(cfg: Config): string[] {
  // A tool is active if enabled; its description comes from defaults.json at
  // runtime, so we don't require one in the config itself.
  const t = (cfg.tools ?? {}) as Record<string, any>;
  return Object.keys(t).filter((k) => t[k]?.enabled === true);
}
// The agent's full capability list for the LLM prompt check: enabled built-in
// tools + the MCP tools it sees (mcp_<name>) from introspection.
export function availableToolNames(cfg: Config, intro: Introspection): string[] {
  const names = [...enabledTools(cfg)];
  for (const s of (cfg.mcp_servers ?? []) as any[]) {
    const info = intro[s.url];
    if (!info || info.error) continue;
    for (const t of info.tools) if (!t.startsWith("polartie_")) names.push(`mcp_${t}`);
  }
  return names;
}

const hasNotif = (c: Config) => !!String(c.notification_service_url ?? "").trim();
const hasPostgres = (c: Config) => !!c.lightrag?.postgres?.host;
const hasOpenSearch = (c: Config) => !!String(c.opensearch_endpoint ?? "").trim();
function host(url: string): string { try { return new URL(url).host.split(".")[0]; } catch { return url; } }

export function configToGraph(cfg: Config, intro: Introspection = {}): Graph {
  const nodes: GNode[] = [];
  const edges: GEdge[] = [];
  nodes.push({
    id: "agent", kind: "agent", status: "ok", col: 0,
    label: cfg.display_name || "Agent",
    sub: `${cfg.custom_config?.gpt_model ?? "gpt-realtime"} · ${cfg.voice ?? "alloy"} · ${cfg.default_language_iso || "—"}`,
  });

  // col 1: tools grouped by category
  const byCat = new Map<string, string[]>();
  for (const t of enabledTools(cfg)) byCat.set(categorize(t), [...(byCat.get(categorize(t)) ?? []), t]);
  let commBroken = false;
  for (const [cat, ts] of byCat) {
    const broken = ts.some((t) => NEEDS_NOTIF.has(t) && !hasNotif(cfg));
    if (broken) commBroken = true;
    nodes.push({ id: `cat:${cat}`, kind: "tool", status: broken ? "error" : "ok", col: 1, label: cat, sub: `${ts.length}: ${ts.slice(0, 2).join(", ")}${ts.length > 2 ? "…" : ""}` });
    edges.push({ from: "agent", to: `cat:${cat}`, status: "ok" });
    if (broken) edges.push({ from: `cat:${cat}`, to: "be:notif", status: "error" });
  }

  // col 1: MCP servers (with introspected tool count)
  (cfg.mcp_servers ?? []).forEach((m: any, i: number) => {
    const info = intro[m.url];
    const n = info && !info.error ? info.tools.filter((t) => !t.startsWith("polartie_")).length : null;
    nodes.push({ id: `mcp:${i}`, kind: "mcp", status: info?.error ? "error" : "info", col: 1, label: `MCP · ${host(m.url)}`, sub: info?.error ? "unreachable" : n !== null ? `${n} tools (list_tools)` : "list_tools" });
    edges.push({ from: "agent", to: `mcp:${i}`, status: info?.error ? "error" : "info" });
  });

  // col 1: knowledge bases → backend
  for (const kb of (cfg.knowledge_base ?? []) as any[]) {
    const mode = kb.index_mode ?? "simple";
    const ok = mode === "lightrag" ? hasPostgres(cfg) : hasOpenSearch(cfg);
    const id = `kb:${kb.knowledge_id ?? kb.function_tag}`;
    nodes.push({ id, kind: "kb", status: ok ? "ok" : "error", col: 1, label: `KB · ${kb.function_tag ?? kb.knowledge_id}`, sub: mode });
    edges.push({ from: "agent", to: id, status: "ok" });
    edges.push({ from: id, to: mode === "lightrag" ? "be:pg" : "be:os", status: ok ? "ok" : "error" });
  }

  // col 2: transfer topics (from the Call control node if transfer is enabled)
  const transferOn = enabledTools(cfg).includes("transfer");
  (cfg.transfer_topics ?? []).forEach((t: any) => {
    const id = `topic:${t.function_tag ?? t.topic_id}`;
    nodes.push({ id, kind: "transfer", status: "ok", col: 2, label: `topic · ${t.function_tag ?? t.topic_id}` });
    edges.push({ from: transferOn ? "cat:Call control" : "agent", to: id, status: "ok" });
  });

  // col 2: backend dependency nodes
  if (commBroken || enabledTools(cfg).some((t) => NEEDS_NOTIF.has(t)))
    nodes.push({ id: "be:notif", kind: "backend", status: hasNotif(cfg) ? "ok" : "error", col: 2, label: "Notification service", sub: hasNotif(cfg) ? "present" : "MISSING — required" });
  if ((cfg.knowledge_base ?? []).some((k: any) => (k.index_mode ?? "simple") === "lightrag"))
    nodes.push({ id: "be:pg", kind: "backend", status: hasPostgres(cfg) ? "ok" : "error", col: 2, label: "Postgres (LightRAG)", sub: hasPostgres(cfg) ? "present" : "MISSING" });
  if ((cfg.knowledge_base ?? []).some((k: any) => (k.index_mode ?? "simple") === "simple"))
    nodes.push({ id: "be:os", kind: "backend", status: hasOpenSearch(cfg) ? "ok" : "error", col: 2, label: "OpenSearch", sub: hasOpenSearch(cfg) ? "present" : "MISSING" });

  return { nodes, edges };
}

export function validateConfig(cfg: Config): Finding[] {
  const out: Finding[] = [];
  const tools = enabledTools(cfg);
  for (const k of tools)
    if (NEEDS_NOTIF.has(k) && !hasNotif(cfg))
      out.push({ level: "error", msg: `${k} is enabled but notification_service_url is empty — it will silently not load.` });
  for (const kb of (cfg.knowledge_base ?? []) as any[]) {
    const mode = kb.index_mode ?? "simple";
    if (mode === "lightrag" && !hasPostgres(cfg)) out.push({ level: "error", msg: `LightRAG KB "${kb.function_tag}" needs lightrag.postgres — not configured.` });
    else if (mode === "simple" && !hasOpenSearch(cfg)) out.push({ level: "error", msg: `Simple KB "${kb.function_tag}" needs opensearch_endpoint — not configured.` });
    else out.push({ level: "ok", msg: `KB "${kb.function_tag}" (${mode}) — backend configured.` });
  }
  // KB tools enabled but no KB / backend configured (real in the example config)
  if (tools.includes("knowledge_base_query_lightrag") && !hasPostgres(cfg) && !(cfg.knowledge_base ?? []).length)
    out.push({ level: "warn", msg: `knowledge_base_query_lightrag is enabled but no knowledge_base entry / lightrag config exists — the tool won't resolve (knowledge here comes from MCP instead).` });
  if (tools.includes("knowledge_base_query") && !hasOpenSearch(cfg) && !(cfg.knowledge_base ?? []).length)
    out.push({ level: "warn", msg: `knowledge_base_query is enabled but no opensearch_endpoint / knowledge_base entry exists — the tool won't resolve.` });
  if (tools.includes("transfer")) {
    const n = (cfg.transfer_topics ?? []).length;
    out.push(n ? { level: "ok", msg: `transfer enabled — ${n} transfer topic${n === 1 ? "" : "s"} defined.` } : { level: "warn", msg: `transfer is enabled but no transfer_topics defined.` });
  }
  return out;
}

// The prod-team feature: introspect the MCP servers and check the prompt against
// the tools they actually provide.
export function promptMcpFindings(cfg: Config, intro: Introspection): Finding[] {
  const out: Finding[] = [];
  const servers = (cfg.mcp_servers ?? []) as any[];
  if (!servers.length) return out;
  const prompt = String(cfg.prompt ?? "");
  const refs = new Set<string>();
  for (const m of prompt.matchAll(/\bmcp_([a-z][a-z0-9_]*)/gi)) refs.add(m[1].toLowerCase());
  const avail = new Map<string, string>(); // tool -> server host
  let introspected = false;
  for (const s of servers) {
    const info = intro[s.url];
    if (!info) continue;
    if (info.error) { out.push({ level: "error", msg: `MCP server ${host(s.url)} could not be introspected: ${info.error}` }); continue; }
    introspected = true;
    for (const t of info.tools) if (!t.startsWith("polartie_") && !avail.has(t)) avail.set(t, host(s.url));
  }
  if (!introspected) { out.push({ level: "warn", msg: `Run list_tools on the MCP servers to check the prompt against the tools they actually provide.` }); return out; }
  for (const r of [...refs]) {
    if (avail.has(r)) out.push({ level: "ok", msg: `Prompt uses mcp_${r} — provided by ${avail.get(r)} ✓` });
    else out.push({ level: "error", msg: `Prompt references mcp_${r}, but no connected MCP server provides it — the agent will fail this call.` });
  }
  const ops = new Set(["health", "get_pipeline_status", "get_documents_status", "agent_session_link"]);
  const extra = [...avail.keys()].filter((t) => !refs.has(t) && !ops.has(t));
  if (extra.length) out.push({ level: "info", msg: `${extra.length} more MCP tool${extra.length === 1 ? "" : "s"} available but not mentioned in the prompt (e.g. ${extra.slice(0, 4).join(", ")}) — the agent won't use them.` });
  return out;
}

export function logicalPromptFindings(cfg: Config): Finding[] {
  const out: Finding[] = [];
  const p = String(cfg.prompt ?? "").toLowerCase();
  const tools = enabledTools(cfg);
  if (/\b(e-?mail|email you|send you)\b/.test(p) && (!tools.includes("send_email") || !hasNotif(cfg)))
    out.push({ level: "error", msg: `The prompt offers to email the caller, but send_email is off/broken — it will promise something it can't do.` });
  if (/\bstay_silent\b/.test(p) && !tools.includes("stay_silent"))
    out.push({ level: "warn", msg: `The prompt tells the agent to call stay_silent, but that tool is not enabled.` });
  if (/\btransfer\b/.test(p) && !tools.includes("transfer"))
    out.push({ level: "warn", msg: `The prompt tells the agent to transfer, but the transfer tool is not enabled.` });
  out.push({ level: "ok", msg: `No blocking self-contradictions detected (heuristic — the real check uses the LLM on the full prompt).` });
  return out;
}
