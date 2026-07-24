// Guided config editing — stage 2 of the config-management feature.
//
// The user edits the config INSIDE the bounds the admin published: every change
// is re-evaluated against the published ruleset live; enum rules become the
// widget itself (the admin's options are the user's dropdown); violated rules
// explain themselves and offer one-click fixes derived from the rule; and a
// config with error-level violations cannot be applied at all. The engine stays
// formal — guidance comes from the rules, not from heuristics.
import { useEffect, useMemo, useState } from "react";
import type { Config } from "../config/configModel";
import { describeRule, evaluateRules, type Rule, type RuleResult } from "../config/rules";
import { deriveFields, type DerivedField } from "../config/configVocab";
import { api } from "../api";

function get(cfg: any, path: string): any {
  return path.split(".").reduce<any>((o, k) => (o == null ? undefined : o[k]), cfg);
}
function setPath(cfg: any, path: string, value: any): any {
  const next = structuredClone(cfg);
  const keys = path.split(".");
  let o = next;
  for (const k of keys.slice(0, -1)) o = o[k] ?? (o[k] = {});
  const last = keys[keys.length - 1];
  if (value === "") delete o[last]; else o[last] = value;
  return next;
}

// tool:a|b → ["a","b"]; anything else → null
const toolsOf = (pred: string): string[] | null =>
  pred.startsWith("tool:") ? pred.slice(5).split("|").map((s) => s.trim()) : null;
const fieldOf = (pred: string): string | null => (pred.startsWith("field:") ? pred.slice(6) : null);

// ---- LLM-assisted edits: the model PROPOSES formal ops; the engine decides --

export type EditOp =
  | { op: "enable_tool"; tool: string }
  | { op: "disable_tool"; tool: string }
  | { op: "set_field"; field: string; value: string }
  | { op: "unset_field"; field: string }
  | { op: "add_mcp_server"; url: string; authorization?: string }
  | { op: "remove_mcp_server"; url: string }
  | { op: "add_kb"; knowledge_id: string; index_mode?: string; function_tag?: string }
  | { op: "remove_kb"; knowledge_id: string }
  | { op: "add_transfer_topic"; topic_id: string; function_tag?: string; prompt?: string }
  | { op: "remove_transfer_topic"; topic_id: string };

export function describeOp(e: EditOp): string {
  switch (e.op) {
    case "enable_tool": return `Enable ${e.tool}`;
    case "disable_tool": return `Disable ${e.tool}`;
    case "set_field": return `Set ${e.field} = "${e.value}"`;
    case "unset_field": return `Clear ${e.field}`;
    case "add_mcp_server": return `Add MCP server ${e.url}`;
    case "remove_mcp_server": return `Remove MCP server ${e.url}`;
    case "add_kb": return `Add knowledge base "${e.knowledge_id}" (${e.index_mode || "simple"})`;
    case "remove_kb": return `Remove knowledge base "${e.knowledge_id}"`;
    case "add_transfer_topic": return `Add transfer topic "${e.topic_id}"`;
    case "remove_transfer_topic": return `Remove transfer topic "${e.topic_id}"`;
  }
}

// Apply ops to a draft; unknown tools/fields/entries are SKIPPED (the LLM must
// not invent atoms — a skipped op is surfaced, never silently applied).
export function applyEdits(draft: Config, edits: EditOp[], allowedFields: Set<string>): { next: Config; applied: EditOp[]; skipped: EditOp[] } {
  let next = draft;
  const applied: EditOp[] = [], skipped: EditOp[] = [];
  const list = (p: string): any[] => (get(next, p) as any[]) ?? [];
  for (const e of edits) {
    switch (e.op) {
      case "enable_tool": case "disable_tool":
        if (!next.tools || !(e.tool in next.tools)) { skipped.push(e); continue; }
        next = setPath(next, `tools.${e.tool}.enabled`, e.op === "enable_tool");
        break;
      case "set_field": case "unset_field":
        if (!allowedFields.has(e.field)) { skipped.push(e); continue; }
        next = setPath(next, e.field, e.op === "set_field" ? e.value : "");
        break;
      case "add_mcp_server":
        if (!e.url.trim() || list("mcp_servers").some((m) => m.url === e.url)) { skipped.push(e); continue; }
        next = setPath(next, "mcp_servers", [...list("mcp_servers"), { url: e.url, ...(e.authorization ? { authorization: e.authorization } : {}) }]);
        break;
      case "remove_mcp_server": {
        const rest = list("mcp_servers").filter((m) => m.url !== e.url);
        if (rest.length === list("mcp_servers").length) { skipped.push(e); continue; }
        next = setPath(next, "mcp_servers", rest);
        break;
      }
      case "add_kb":
        if (!e.knowledge_id.trim() || list("knowledge_base").some((k) => k.knowledge_id === e.knowledge_id)) { skipped.push(e); continue; }
        next = setPath(next, "knowledge_base", [...list("knowledge_base"), {
          knowledge_id: e.knowledge_id, index_mode: e.index_mode === "lightrag" ? "lightrag" : "simple",
          function_tag: e.function_tag || e.knowledge_id, prompt: "",
        }]);
        break;
      case "remove_kb": {
        const rest = list("knowledge_base").filter((k) => k.knowledge_id !== e.knowledge_id);
        if (rest.length === list("knowledge_base").length) { skipped.push(e); continue; }
        next = setPath(next, "knowledge_base", rest);
        break;
      }
      case "add_transfer_topic":
        if (!e.topic_id.trim() || list("transfer_topics").some((t) => t.topic_id === e.topic_id)) { skipped.push(e); continue; }
        next = setPath(next, "transfer_topics", [...list("transfer_topics"), {
          topic_id: e.topic_id, function_tag: e.function_tag || e.topic_id, prompt: e.prompt || "",
        }]);
        break;
      case "remove_transfer_topic": {
        const rest = list("transfer_topics").filter((t) => t.topic_id !== e.topic_id);
        if (rest.length === list("transfer_topics").length) { skipped.push(e); continue; }
        next = setPath(next, "transfer_topics", rest);
        break;
      }
    }
    applied.push(e);
  }
  return { next, applied, skipped };
}

// One-click fixes derived from a violated rule — the "guided" part.
interface Fix { label: string; apply: (draft: Config) => Config }
function fixesFor(res: RuleResult, draft: Config): Fix[] {
  const r = res.rule;
  const disable = (names: string[]): Fix[] =>
    names.filter((n) => draft.tools?.[n]?.enabled).map((n) => ({
      label: `Disable ${n}`,
      apply: (d) => setPath(d, `tools.${n}.enabled`, false),
    }));
  if (r.kind === "requires") return disable(toolsOf(r.when) ?? []);
  if (r.kind === "conflicts") return [...disable(toolsOf(r.a) ?? []), ...disable(toolsOf(r.b) ?? [])];
  return r.options.length ? [{ label: `Set ${r.field} = "${r.options[0]}"`, apply: (d) => setPath(d, r.field, r.options[0]) }] : [];
}

export default function GuidedEditor({ cfg, rules, rulesetLabel, onApply }: {
  cfg: Config; rules: Rule[]; rulesetLabel: string; onApply: (next: Config) => void;
}) {
  const [draft, setDraft] = useState<Config>(() => structuredClone(cfg));
  const [showAdvanced, setShowAdvanced] = useState(false);
  useEffect(() => { setDraft(structuredClone(cfg)); }, [cfg]);

  const results = useMemo(() => evaluateRules(draft, rules), [draft, rules]);
  const violated = results.filter((r) => r.state === "violated");
  const errors = violated.filter((r) => r.rule.level === "error");
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(cfg), [draft, cfg]);

  // Fields DERIVED from the loaded config (not hardcoded). A field a rule
  // references but the config lacks is still surfaced (deriveFields adds the
  // common requirable ones); the allow-set also includes any rule-referenced
  // field so the assistant may set one the walk didn't reach.
  const fields = useMemo(() => deriveFields(draft), [draft]);
  const allowedFields = useMemo(() => {
    const s = new Set(fields.map((f) => f.path));
    for (const r of rules) {
      if (r.kind === "enum") s.add(r.field);
      if (r.kind === "requires") { const f = fieldOf(r.needs); if (f) s.add(f); }
    }
    return s;
  }, [fields, rules]);
  const visibleFields = fields.filter((f) => showAdvanced || !f.advanced);
  const advancedCount = fields.filter((f) => f.advanced).length;

  // Enum rules drive their field's widget: the admin's options ARE the choices.
  const enumFor = (field: string) => rules.find((r): r is Extract<Rule, { kind: "enum" }> => r.kind === "enum" && r.field === field);
  // Fields a violated requires-rule needs — highlighted so the user sees where to act.
  const neededFields = new Set(violated.flatMap((v) => v.rule.kind === "requires" ? [fieldOf(v.rule.needs)].filter(Boolean) : []));

  const toggleTool = (name: string) => setDraft((d) => setPath(d, `tools.${name}.enabled`, !d.tools?.[name]?.enabled));
  const toolNames = Object.keys(draft.tools ?? {}).sort();

  // ---- complex structures (lists) ----
  const listOf = (p: string): any[] => (get(draft, p) as any[]) ?? [];
  const setList = (p: string, arr: any[]) => setDraft((d) => setPath(d, p, arr));
  const patchAt = (p: string, i: number, k: string, v: string) =>
    setList(p, listOf(p).map((it, j) => (j === i ? { ...it, [k]: v } : it)));
  const dropAt = (p: string, i: number) => setList(p, listOf(p).filter((_, j) => j !== i));

  // ---- connector registry reuse: define a connection once (D-10), use it in
  // SOP data bindings AND here in the agent config. Rows added from a connector
  // keep a {connector} reference + "secret:<name>" auth — the write-back
  // (/config/render-robot) resolves them; raw credentials never enter the editor.
  const [connectors, setConnectors] = useState<Array<{ name: string; kind: string; enabled: boolean; config: any }>>([]);
  const loadConnectors = () => api<any[]>("GET", "/connectors").then(setConnectors).catch(() => { /* offline — picker hidden */ });
  useEffect(() => { loadConnectors(); }, []);
  const mcpConnectors = connectors.filter((c) => c.kind === "mcp");

  const addFromConnector = (name: string) => {
    const c = mcpConnectors.find((x) => x.name === name);
    if (!c || listOf("mcp_servers").some((m) => m.connector === name)) return;
    setList("mcp_servers", [...listOf("mcp_servers"), {
      url: c.config?.server ?? "", connector: c.name,
      ...(c.config?.auth_secret ? { authorization: `secret:${c.config.auth_secret}` } : {}),
    }]);
  };

  // Promote an ad-hoc URL row to a named registry connector (so SOPs can bind it too).
  const promote = async (i: number) => {
    const m = listOf("mcp_servers")[i];
    if (!m?.url?.trim()) return;
    let name = "mcp";
    try { name = new URL(m.url).hostname.split(".")[0] || "mcp"; } catch { /* keep fallback */ }
    try {
      await api("PUT", `/connectors/${name}`, {
        kind: "mcp", description: "registered from the config editor", config: { server: m.url },
      });
      patchAt("mcp_servers", i, "connector", name);
      await loadConnectors();
    } catch { /* surfaced by the registry views; row stays ad-hoc */ }
  };

  // ---- LLM-assisted edits ("change X for me") ----
  const [ask, setAsk] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [askErr, setAskErr] = useState("");
  const [proposal, setProposal] = useState<{
    note: string; applied: EditOp[]; skipped: EditOp[]; next: Config; blocking: RuleResult[]; warns: RuleResult[];
  } | null>(null);

  const propose = async () => {
    if (!ask.trim()) return;
    setAskBusy(true); setAskErr(""); setProposal(null);
    try {
      const r = await api<{ edits?: EditOp[]; reply?: string; note?: string; error?: string }>("POST", "/config/draft-edit", {
        instruction: ask,
        tools: toolNames.map((t) => ({ name: t, enabled: draft.tools?.[t]?.enabled === true })),
        fields: [...allowedFields].map((f) => ({ field: f, value: get(draft, f) ?? null, options: enumFor(f)?.options })),
        rules: rules.map(describeRule),
        structures: {
          mcp_servers: listOf("mcp_servers").map((m) => String(m.url ?? "")),
          knowledge_bases: listOf("knowledge_base").map((k) => ({ knowledge_id: String(k.knowledge_id ?? ""), index_mode: String(k.index_mode ?? "simple") })),
          transfer_topics: listOf("transfer_topics").map((t) => String(t.topic_id ?? "")),
        },
      });
      if (r.error) { setAskErr(r.error); return; }
      const reply = r.reply || r.note || "";
      // Answer-only (a question, or a request that needs something outside the
      // editor's vocabulary): show the reply conversationally, no edits to stage.
      if (!r.edits?.length) {
        setProposal({ note: reply, applied: [], skipped: [], next: draft, blocking: [], warns: [] });
        return;
      }
      // The gate: evaluate the admin ruleset on the EDITED draft before offering
      // it — judged on the violations the proposal INTRODUCES (pre-existing
      // draft violations are the editor's business, not the proposal's).
      const { next, applied, skipped } = applyEdits(draft, r.edits, allowedFields);
      const before = new Set(evaluateRules(draft, rules).filter((res) => res.state === "violated").map((res) => res.rule.id));
      const evald = evaluateRules(next, rules).filter((res) => res.state === "violated" && !before.has(res.rule.id));
      setProposal({
        note: reply, applied, skipped, next,
        blocking: evald.filter((v) => v.rule.level === "error"),
        warns: evald.filter((v) => v.rule.level !== "error"),
      });
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setAskErr(m.includes("Not Found") ? "Assistant endpoint not found — restart the backend for /config/draft-edit." : `Assistant failed: ${m}`);
    } finally { setAskBusy(false); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className="sub">Every edit is checked against <b style={{ color: "var(--text2)" }}>{rulesetLabel}</b> before it can be applied.</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          {dirty && <span className="chip warn"><span className="cd" />unsaved edits</span>}
          {errors.length > 0
            ? <span className="chip crit"><span className="cd" />{errors.length} blocking</span>
            : violated.length > 0
              ? <span className="chip warn"><span className="cd" />{violated.length} warning{violated.length === 1 ? "" : "s"}</span>
              : <span className="chip good"><span className="cd" />within bounds</span>}
          <button className="btn ghost sm" onClick={() => setDraft(structuredClone(cfg))} disabled={!dirty}>Reset</button>
          <button className="btn sm primary" onClick={() => onApply(draft)} disabled={!dirty || errors.length > 0}
            title={errors.length ? "Fix the blocking violations first — the admin ruleset forbids this config." : ""}>
            Apply changes
          </button>
        </span>
      </div>

      {/* Staged-vs-committed is the #1 confusion: edits live in this draft until
          "Apply changes" writes them to the config (which updates the graph/JSON
          above). Make that explicit whenever the draft diverges. */}
      {dirty && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "8px 12px",
          background: "var(--panel2, rgba(127,127,127,.08))", border: "1px solid var(--warn)", borderRadius: 8 }}>
          <span style={{ color: "var(--warn)", fontWeight: 700 }}>●</span>
          <span style={{ flex: 1, minWidth: 220, fontSize: 12.5 }}>
            You have unsaved edits staged here. {errors.length > 0
              ? <b style={{ color: "var(--crit)" }}>Fix the blocking violation to enable Apply.</b>
              : <>Click <b>Apply changes</b> to write them to the config (updating the graph &amp; JSON), or <b>Reset</b> to discard.</>}
          </span>
          <button className="btn ghost sm" onClick={() => setDraft(structuredClone(cfg))}>Reset</button>
          <button className="btn sm primary" onClick={() => onApply(draft)} disabled={errors.length > 0}>Apply changes</button>
        </div>
      )}

      {/* the violations panel IS the guidance: each violated rule explains itself and offers derived fixes */}
      {violated.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "8px 10px", background: "var(--panel2, rgba(127,127,127,.06))", borderRadius: 8 }}>
          {violated.map((res) => (
            <div key={res.rule.id} className="lintline" style={{ display: "flex", gap: 9, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ color: res.rule.level === "error" ? "var(--crit)" : "var(--warn)", fontWeight: 700 }}>
                {res.rule.level === "error" ? "✖" : "⚠"}
              </span>
              <span style={{ flex: 1, minWidth: 200 }}>{res.rule.msg}</span>
              {fixesFor(res, draft).map((f) => (
                <button key={f.label} className="btn ghost sm" onClick={() => setDraft(f.apply(draft))}>{f.label}</button>
              ))}
            </div>
          ))}
        </div>
      )}

      {/* assistant: plain English → formal ops, gated by the same ruleset */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "10px 12px", border: "1px solid var(--line)", borderRadius: 8 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input className="area" style={{ flex: 1 }} placeholder='ask or change — e.g. "how do I add weather data?" or "let the agent send verification SMS"'
            value={ask} onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !askBusy && propose()} />
          <button className="btn sm primary" onClick={propose} disabled={askBusy || !ask.trim()}>{askBusy ? "Thinking…" : "Propose"}</button>
        </div>
        {askErr && <div className="lintline" style={{ color: "var(--crit)" }}>{askErr}</div>}
        {proposal && (() => {
          const answerOnly = proposal.applied.length === 0 && proposal.skipped.length === 0 && proposal.blocking.length === 0;
          return (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "8px 10px", background: "var(--panel2, rgba(127,127,127,.06))", borderRadius: 8 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              {answerOnly
                ? <span className="chip muted" style={{ flex: "0 0 auto" }}><span className="cd" />answer</span>
                : proposal.blocking.length > 0
                  ? <span className="chip crit" style={{ flex: "0 0 auto" }}><span className="cd" />violates admin rules</span>
                  : proposal.warns.length > 0
                    ? <span className="chip warn" style={{ flex: "0 0 auto" }}><span className="cd" />{proposal.warns.length} warning{proposal.warns.length === 1 ? "" : "s"}</span>
                    : <span className="chip good" style={{ flex: "0 0 auto" }}><span className="cd" />within bounds</span>}
              {proposal.note && <span style={{ flex: 1, fontSize: 12.5, lineHeight: 1.45 }}>{proposal.note}</span>}
            </div>
            {proposal.applied.map((e, i) => (
              <div key={i} className="lintline mono" style={{ fontSize: 12, color: "var(--text2)" }}>→ {describeOp(e)}</div>
            ))}
            {proposal.skipped.map((e, i) => (
              <div key={"s" + i} className="lintline" style={{ fontSize: 12, color: "var(--muted)" }}>✕ skipped (unknown atom): {describeOp(e)}</div>
            ))}
            {proposal.blocking.map((v) => (
              <div key={v.rule.id} className="lintline" style={{ fontSize: 12, color: "var(--crit)" }}>✖ {v.rule.msg}</div>
            ))}
            <div style={{ display: "flex", gap: 8, marginTop: 2, alignItems: "center" }}>
              {proposal.applied.length > 0 && proposal.blocking.length === 0 && (
                <button className="btn sm primary" onClick={() => { setDraft(proposal.next); setProposal(null); setAsk(""); }}
                  title="Stage these edits in the editor below — then Apply changes to write them to the config">
                  Add to edits
                </button>
              )}
              <button className="btn ghost sm" onClick={() => setProposal(null)}>{answerOnly ? "Dismiss" : "Discard"}</button>
              {proposal.applied.length > 0 && proposal.blocking.length === 0 && <span className="sub">stages the edits — review, then Apply changes</span>}
              {proposal.blocking.length > 0 && <span className="sub">The admin's ruleset forbids this change — it cannot be applied.</span>}
            </div>
          </div>
          );
        })()}
      </div>

      <div className="grid2">
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            Tools ({toolNames.filter((t) => draft.tools?.[t]?.enabled).length} enabled)
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {toolNames.map((t) => {
              const on = draft.tools?.[t]?.enabled === true;
              return (
                <button key={t} className={"chip " + (on ? "accent" : "muted")} onClick={() => toggleTool(t)}
                  style={{ cursor: "pointer", opacity: on ? 1 : 0.6 }} title={on ? "Click to disable" : "Click to enable"}>
                  <span className="cd" />{t}
                </button>
              );
            })}
          </div>
        </div>
        <div>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)" }}>
              Fields ({visibleFields.length}) <span className="sub" style={{ textTransform: "none", fontWeight: 400 }}>· from the loaded config</span>
            </span>
            {advancedCount > 0 && (
              <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => setShowAdvanced((s) => !s)}>
                {showAdvanced ? "Hide" : "Show"} advanced ({advancedCount})
              </button>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {visibleFields.map((fld: DerivedField) => {
              const f = fld.path;
              const en = enumFor(f);
              const v = get(draft, f);
              const needed = neededFields.has(f);
              const setVal = (raw: string) =>
                setDraft(setPath(draft, f, fld.type === "number" ? (raw === "" ? "" : Number(raw)) : raw));
              return (
                <label key={f} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12 }}>
                  <span className="mono" style={{ flex: "0 0 200px", color: needed ? "var(--crit)" : "var(--muted)" }}
                    title={`${fld.type}${fld.advanced ? " · advanced" : ""}`}>
                    {f}{needed ? " ←" : ""}
                  </span>
                  {en ? (
                    // the admin's enum rule bounds the widget itself
                    <select className="area mono" style={{ flex: 1, padding: "4px 8px" }} value={String(v ?? "")}
                      onChange={(e) => setVal(e.target.value)}>
                      {!en.options.includes(String(v ?? "")) && <option value={String(v ?? "")}>{String(v ?? "(unset)")} — not allowed</option>}
                      {en.options.map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  ) : fld.type === "boolean" ? (
                    <select className="area mono" style={{ width: "auto", padding: "4px 8px" }} value={v === true ? "true" : "false"}
                      onChange={(e) => setDraft(setPath(draft, f, e.target.value === "true"))}>
                      <option value="true">true</option><option value="false">false</option>
                    </select>
                  ) : (
                    <input className="area mono" type={fld.type === "number" ? "number" : "text"}
                      style={{ flex: 1, padding: "4px 8px", borderColor: needed ? "var(--crit)" : undefined }}
                      value={v == null ? "" : String(v)} placeholder="(unset)"
                      onChange={(e) => setVal(e.target.value)} />
                  )}
                </label>
              );
            })}
          </div>
        </div>
      </div>

      {/* complex structures — each edit re-evaluates the ruleset live (kb index_mode
          drives kb_mode:* rules; the topic list drives field:transfer_topics) */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            MCP servers ({listOf("mcp_servers").length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {listOf("mcp_servers").map((m, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                {m.connector && <span className="chip accent" title="Bound to the connector registry — the write-back resolves url/auth from it"><span className="cd" />{m.connector}</span>}
                <input className="area mono" style={{ flex: 2, minWidth: 180, padding: "4px 8px" }} placeholder="https://…/mcp"
                  value={m.url ?? ""} onChange={(e) => patchAt("mcp_servers", i, "url", e.target.value)} />
                <input className="area mono" style={{ flex: 1, minWidth: 120, padding: "4px 8px" }}
                  placeholder="authorization or secret:<name>" title='Use "secret:<name>" to reference a tenant secret — resolved at write-back, never shown here'
                  value={m.authorization ?? ""} onChange={(e) => patchAt("mcp_servers", i, "authorization", e.target.value)} />
                {!m.connector && (m.url ?? "").trim() && (
                  <button className="btn ghost sm" title="Register in the connector registry so SOPs can bind it by name" onClick={() => promote(i)}>Save as connector</button>
                )}
                <button className="btn ghost sm" title="Remove server" onClick={() => dropAt("mcp_servers", i)}>✕</button>
              </div>
            ))}
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button className="btn ghost sm" onClick={() => setList("mcp_servers", [...listOf("mcp_servers"), { url: "" }])}>+ Add MCP server</button>
              {mcpConnectors.length > 0 && (
                <select className="area mono" style={{ width: "auto", padding: "4px 8px" }} value=""
                  title="Reuse a connection defined once in the connector registry"
                  onChange={(e) => { if (e.target.value) addFromConnector(e.target.value); e.target.value = ""; }}>
                  <option value="">+ from connector…</option>
                  {mcpConnectors.map((c) => (
                    <option key={c.name} value={c.name} disabled={listOf("mcp_servers").some((m) => m.connector === c.name)}>
                      {c.name} — {c.config?.server ?? "?"}{c.enabled ? "" : " (disabled)"}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            Knowledge bases ({listOf("knowledge_base").length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {listOf("knowledge_base").map((k, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <input className="area mono" style={{ flex: 1, minWidth: 120, padding: "4px 8px" }} placeholder="knowledge_id"
                  value={k.knowledge_id ?? ""} onChange={(e) => patchAt("knowledge_base", i, "knowledge_id", e.target.value)} />
                <select className="area mono" style={{ width: "auto", padding: "4px 8px" }}
                  value={k.index_mode ?? "simple"} onChange={(e) => patchAt("knowledge_base", i, "index_mode", e.target.value)}>
                  <option value="simple">simple (OpenSearch)</option>
                  <option value="lightrag">lightrag (Postgres)</option>
                </select>
                <input className="area mono" style={{ flex: 1, minWidth: 100, padding: "4px 8px" }} placeholder="function_tag"
                  value={k.function_tag ?? ""} onChange={(e) => patchAt("knowledge_base", i, "function_tag", e.target.value)} />
                <input className="area" style={{ flex: 2, minWidth: 160, padding: "4px 8px" }} placeholder="prompt (when to use it)"
                  value={k.prompt ?? ""} onChange={(e) => patchAt("knowledge_base", i, "prompt", e.target.value)} />
                <button className="btn ghost sm" title="Remove knowledge base" onClick={() => dropAt("knowledge_base", i)}>✕</button>
              </div>
            ))}
            <button className="btn ghost sm" style={{ alignSelf: "flex-start" }}
              onClick={() => setList("knowledge_base", [...listOf("knowledge_base"), { knowledge_id: "", index_mode: "simple", function_tag: "", prompt: "" }])}>
              + Add knowledge base</button>
          </div>
        </div>

        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            Transfer topics ({listOf("transfer_topics").length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {listOf("transfer_topics").map((t, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <input className="area mono" style={{ flex: 1, minWidth: 110, padding: "4px 8px" }} placeholder="topic_id"
                  value={t.topic_id ?? ""} onChange={(e) => patchAt("transfer_topics", i, "topic_id", e.target.value)} />
                <input className="area mono" style={{ flex: 1, minWidth: 100, padding: "4px 8px" }} placeholder="function_tag"
                  value={t.function_tag ?? ""} onChange={(e) => patchAt("transfer_topics", i, "function_tag", e.target.value)} />
                <input className="area" style={{ flex: 2, minWidth: 160, padding: "4px 8px" }} placeholder="prompt (when to transfer here)"
                  value={t.prompt ?? ""} onChange={(e) => patchAt("transfer_topics", i, "prompt", e.target.value)} />
                <button className="btn ghost sm" title="Remove topic" onClick={() => dropAt("transfer_topics", i)}>✕</button>
              </div>
            ))}
            <button className="btn ghost sm" style={{ alignSelf: "flex-start" }}
              onClick={() => setList("transfer_topics", [...listOf("transfer_topics"), { topic_id: "", function_tag: "", prompt: "" }])}>
              + Add transfer topic</button>
          </div>
        </div>
      </div>
    </div>
  );
}
