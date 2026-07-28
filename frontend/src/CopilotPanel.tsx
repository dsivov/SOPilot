// The unified SOPilot copilot — ONE assistant across every Studio tab.
//
// Mounted once in App (a sibling of the view-switch), so it stays mounted and its
// conversation persists across tab switches. History and memory are DB-backed
// (per tenant/project), so they also survive reloads. It is state-aware,
// role-aware, and enforces the bounding chain (Stage 1 ⊂ Stage 0, Stage 2 ⊂
// Stage 1) — all in the backend brain (/copilot/*).
import { Brain, Eraser, Send, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useCopilotBridge, type CopilotProposal } from "./copilot/bridge";

type Warning = { level: "warn" | "error"; msg: string };
type DiscItem = { name?: string; description?: string; args?: string[]; method?: string; path?: string; summary?: string };
type Discovered = { kind: string; base_url?: string; count: number; items?: DiscItem[] };
type Msg = { role: "user" | "assistant"; content: string; tab?: string; warnings?: Warning[]; remembered?: string[]; proposal?: CopilotProposal | null; discovered?: Discovered | null; showAll?: boolean; applied?: boolean; system?: boolean };

// Build a connector proposal from a discovered item — so the operator can pick
// ANY tool/endpoint, not just the copilot's recommendation.
function connectorFromItem(d: Discovered, it: DiscItem): CopilotProposal {
  if (d.kind === "mcp") {
    const qa = (it.args ?? []).find((a) => /^(query|question|text|prompt|q|search)$/i.test(a)) ?? (it.args ?? [])[0];
    return { kind: "connector", summary: `Use ${it.name}`, payload: { kind: "mcp", name: it.name, description: it.description ?? "", config: { url: d.base_url ?? "", tool: it.name, ...(qa ? { query_arg: qa } : {}) } } };
  }
  const leaf = (it.path ?? "").split("/").filter(Boolean).pop() ?? "endpoint";
  return { kind: "connector", summary: `Use ${it.method} ${it.path}`, payload: { kind: "http", name: leaf.replace(/[^a-z0-9_]+/gi, "_").toLowerCase(), description: it.summary ?? "", config: { url: (d.base_url ?? "") + (it.path ?? ""), method: it.method ?? "GET" } } };
}
type Mem = { id: string; kind: string; title: string; content: string; source: string; tenant_wide: boolean; updated_at: string };

// view id → the label the copilot shows as "you're on …"
const TAB_LABEL: Record<string, string> = {
  sops: "SOPs", blocks: "Prompt blocks", connectors: "Connectors",
  configAdmin: "Config admin (Stage 1)", config: "Config viewer (Stage 2)",
  dashboard: "Dashboard", playground: "Playground", sessions: "Sessions", traces: "Traces",
};

export default function CopilotPanel({ view, project }: { view: string; project: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [ask, setAsk] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [memory, setMemory] = useState<Mem[]>([]);
  const [memOpen, setMemOpen] = useState(false);
  const [role, setRole] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const bridge = useCopilotBridge();

  const loadThread = useCallback(() => {
    api<{ messages: Msg[] }>("GET", "/copilot/thread")
      .then((r) => setMsgs((r.messages || []).map((m) => {
        const meta = (m as { meta?: { warnings?: Warning[]; discovered?: Discovered; proposal?: CopilotProposal } }).meta;
        return { role: m.role, content: m.content, tab: m.tab, warnings: meta?.warnings, discovered: meta?.discovered, proposal: meta?.proposal };
      })))
      .catch(() => { /* offline / not migrated — start empty */ });
  }, []);
  const loadMemory = useCallback(() => {
    api<{ memories: Mem[] }>("GET", "/copilot/memory").then((r) => setMemory(r.memories || [])).catch(() => {});
  }, []);

  // Load (and reload when the project changes) — history + memory are per project/tenant.
  useEffect(() => { loadThread(); loadMemory(); }, [project, loadThread, loadMemory]);
  useEffect(() => { if (open && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [msgs, busy, open]);

  const send = async () => {
    const q = ask.trim();
    if (!q || busy) return;
    setAsk("");
    setMsgs((m) => [...m, { role: "user", content: q, tab: view }]);
    setBusy(true);
    try {
      const snapshot = (bridge?.getSnapshot() as Record<string, unknown>) ?? {};
      const r = await api<{ reply?: string; warnings?: Warning[]; remembered?: string[]; role?: string; proposal?: CopilotProposal | null; discovered?: Discovered | null; error?: string }>(
        "POST", "/copilot/assist", { tab: view, instruction: q, snapshot });
      if (r.error) { setMsgs((m) => [...m, { role: "assistant", content: r.error!, warnings: [{ level: "error", msg: r.error! }] }]); return; }
      if (r.role) setRole(r.role);
      setMsgs((m) => [...m, { role: "assistant", content: r.reply || "", warnings: r.warnings, remembered: r.remembered, proposal: r.proposal, discovered: r.discovered }]);
      if (r.warnings && r.warnings.length) bridge?.getReport()?.(r.warnings);  // surface findings in the active view
      if (r.remembered && r.remembered.length) loadMemory();
    } catch (e: unknown) {
      const msg = String((e as { message?: string })?.message ?? e);
      setMsgs((m) => [...m, { role: "assistant", content: msg.includes("Not Found") ? "Copilot endpoint not found — restart the backend." : `Copilot failed: ${msg}`, warnings: [{ level: "error", msg }] }]);
    } finally { setBusy(false); }
  };

  const clearThread = async () => {
    if (!window.confirm("Clear this conversation? Gathered memory is kept.")) return;
    await api("DELETE", "/copilot/thread").catch(() => {});
    setMsgs([]);
  };
  const deleteMemory = async (id: string) => {
    await api("DELETE", `/copilot/memory/${id}`).catch(() => {});
    setMemory((m) => m.filter((x) => x.id !== id));
  };
  const resetMemory = async () => {
    if (!window.confirm("Delete ALL gathered copilot memory for this tenant? Use this if stale context is causing problems. This can't be undone.")) return;
    await api("DELETE", "/copilot/memory").catch(() => {});
    setMemory([]);
  };

  const applyProposal = (idx: number) => {
    const p = msgs[idx]?.proposal;
    if (!p) return;
    const fn = bridge?.getApply();
    if (!fn) {
      setMsgs((m) => m.concat([{ role: "assistant", system: true, content: `Open the relevant tab to apply this (${p.summary || p.kind}).` }]));
      return;
    }
    const note = fn(p);
    setMsgs((m) => m.map((x, i) => (i === idx ? { ...x, applied: true } : x))
      .concat([{ role: "assistant", system: true, content: note || "Applied to the editor — review and save." }]));
  };

  const applyItem = (d: Discovered, it: DiscItem) => {
    const p = connectorFromItem(d, it);
    const fn = bridge?.getApply();
    const note = fn ? fn(p) : null;
    setMsgs((m) => m.concat([{ role: "assistant", system: true, content: note || `Open the Connectors tab to apply "${p.summary}".` }]));
  };
  const toggleShowAll = (idx: number) => setMsgs((m) => m.map((x, i) => (i === idx ? { ...x, showAll: !x.showAll } : x)));

  const wchip = (w: Warning, i: number) => (
    <div key={i} style={{ fontSize: 11.5, marginTop: 3, color: w.level === "error" ? "var(--crit)" : "var(--warn)" }}>
      {w.level === "error" ? "✖" : "⚠"} {w.msg}
    </div>
  );

  if (!open) {
    return (
      <button className="btn primary" onClick={() => setOpen(true)}
        style={{ position: "fixed", right: 20, bottom: 20, zIndex: 60, borderRadius: 999, boxShadow: "0 6px 24px rgba(0,0,0,.35)" }}>
        <Sparkles size={15} /> SOPilot copilot{msgs.filter((m) => m.role === "user").length ? ` (${msgs.filter((m) => m.role === "user").length})` : ""}
      </button>
    );
  }

  return (
    <div style={{ position: "fixed", right: 20, bottom: 20, zIndex: 60, width: 430, maxWidth: "calc(100vw - 40px)",
      height: "min(640px, 86vh)", display: "flex", flexDirection: "column",
      background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 12, boxShadow: "0 12px 44px rgba(0,0,0,.4)" }}>
      <div className="chead" style={{ borderBottom: "1px solid var(--line)", padding: "10px 12px", display: "flex", alignItems: "center", gap: 6 }}>
        <Sparkles size={16} /><span style={{ fontWeight: 600 }}>SOPilot copilot</span>
        {role && <span className="chip" style={{ fontSize: 10.5 }}>{role}</span>}
        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button className="btn ghost sm" title="Gathered memory" onClick={() => setMemOpen((v) => !v)}><Brain size={14} />{memory.length ? ` ${memory.length}` : ""}</button>
          {msgs.length > 0 && <button className="btn ghost sm" title="Clear conversation (keeps memory)" onClick={clearThread}><Eraser size={14} /></button>}
          <button className="btn ghost sm" title="Close" onClick={() => setOpen(false)}><X size={14} /></button>
        </span>
      </div>
      <div style={{ padding: "5px 12px", borderBottom: "1px solid var(--line)", fontSize: 11.5, color: "var(--muted)" }}>
        on <b style={{ color: "var(--text2)" }}>{TAB_LABEL[view] ?? view}</b> · knows your published config, rules &amp; discovery · remembers this tenant
      </div>

      {memOpen && (
        <div style={{ borderBottom: "1px solid var(--line)", maxHeight: 200, overflowY: "auto", padding: "8px 12px", background: "var(--panel2, rgba(127,127,127,.06))" }}>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 6 }}>
            <b style={{ fontSize: 12 }}>Gathered memory ({memory.length})</b>
            {memory.length > 0 && <button className="btn ghost sm" style={{ marginLeft: "auto", color: "var(--crit)" }} onClick={resetMemory}>Reset all</button>}
          </div>
          {memory.length === 0 && <div className="sub" style={{ fontSize: 11.5 }}>Nothing remembered yet — the copilot saves durable facts (discoveries, decisions, preferences) here as you work.</div>}
          {memory.map((m) => (
            <div key={m.id} style={{ display: "flex", gap: 6, alignItems: "flex-start", padding: "4px 0", borderTop: "1px solid var(--line)" }}>
              <span className="chip" style={{ fontSize: 10 }}>{m.kind}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600 }}>{m.title}</div>
                <div className="sub" style={{ fontSize: 11.5 }}>{m.content}</div>
              </div>
              <button className="btn ghost sm" title="Forget this" onClick={() => deleteMemory(m.id)}><Trash2 size={12} /></button>
            </div>
          ))}
        </div>
      )}

      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
        {msgs.length === 0 && (
          <div className="sub" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
            Ask me anything about building on this platform — how a feature works, what options you have here, or to check
            what you've configured. I stay within your role's bounds and warn about problems.
          </div>
        )}
        {msgs.map((m, i) => m.role === "user" ? (
          <div key={i} style={{ alignSelf: "flex-end", maxWidth: "88%", background: "var(--accent-soft, rgba(59,110,245,.12))",
            borderRadius: "10px 10px 2px 10px", padding: "7px 10px", fontSize: 12.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{m.content}</div>
        ) : m.system ? (
          <div key={i} className="sub" style={{ alignSelf: "flex-start", fontSize: 11.5, color: "var(--muted)", padding: "2px 4px" }}>✓ {m.content}</div>
        ) : (
          <div key={i} style={{ alignSelf: "flex-start", maxWidth: "92%", background: "var(--panel2, rgba(127,127,127,.1))",
            borderRadius: "10px 10px 10px 2px", padding: "8px 10px", fontSize: 12.5, color: "var(--text2)", lineHeight: 1.45, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {m.content}
            {(m.warnings ?? []).map(wchip)}
            {m.proposal && (
              <div style={{ marginTop: 8, border: "1px solid var(--accent, #3b6ef5)", borderRadius: 8, padding: "7px 9px", background: "var(--surface)" }}>
                <div style={{ fontSize: 11.5, marginBottom: 5 }}><b>{m.proposal.kind === "connector" ? "★ Recommended:" : "Proposed:"}</b> {m.proposal.summary || m.proposal.kind}</div>
                {m.applied
                  ? <span className="sub" style={{ fontSize: 11 }}>applied ✓</span>
                  : <button className="btn sm primary" onClick={() => applyProposal(i)}>Apply to editor</button>}
              </div>
            )}
            {m.discovered && m.discovered.kind !== "none" && (() => {
              const d = m.discovered!; const items = d.items ?? [];
              const shown = m.showAll ? items : items.slice(0, 4);
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 5, flexWrap: "wrap" }}>
                    <span className="chip good" style={{ fontSize: 11 }}><span className="cd" />{d.count} {d.kind} item{d.count === 1 ? "" : "s"} found</span>
                    {items.length > 4 && <button className="btn ghost sm" onClick={() => toggleShowAll(i)}>{m.showAll ? "Show fewer" : `Browse all ${d.count}`}</button>}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                    {shown.map((it, j) => {
                      const label = d.kind === "mcp" ? it.name : `${it.method} ${it.path}`;
                      const desc = d.kind === "mcp" ? it.description : it.summary;
                      return (
                        <div key={j} style={{ border: "1px solid var(--line)", borderRadius: 8, padding: "6px 8px", background: "var(--surface)" }}>
                          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                            <b className="mono" style={{ fontSize: 11.5, wordBreak: "break-all" }}>{label}</b>
                            <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => applyItem(d, it)}>Use this</button>
                          </div>
                          {desc && <div className="sub" style={{ fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>{desc}</div>}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
            {(m.remembered?.length ?? 0) > 0 && <div className="sub" style={{ fontSize: 11, marginTop: 4 }}>🧠 remembered: {m.remembered!.join(", ")}</div>}
          </div>
        ))}
        {busy && <div className="sub" style={{ fontSize: 12 }}>Thinking…</div>}
      </div>

      <div style={{ display: "flex", gap: 6, padding: "10px 12px", borderTop: "1px solid var(--line)" }}>
        <input className="qinput" style={{ flex: 1 }} placeholder={`Ask about ${TAB_LABEL[view] ?? view}…`} value={ask}
          onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} />
        <button className="btn sm primary" onClick={send} disabled={busy || !ask.trim()}><Send size={14} /></button>
      </div>
    </div>
  );
}
