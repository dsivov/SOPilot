// Connector registry (D-10): configure, monitor, and live-test the retrieval
// systems behind background prefetch. Connection details live here; SOP stages
// bind by name via data_dependencies[].config.connector.
import { KeyRound, Plug, Save, Sparkles, Send, Trash2, X, Zap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type Suggestion = { kind: string; name: string; description: string; config: Record<string, unknown> };
type DiscItem = { name?: string; description?: string; args?: string[]; method?: string; path?: string; summary?: string };
type Discovered = { kind: string; base_url?: string; count: number; items?: DiscItem[] };
type ChatMsg = {
  role: "user" | "assistant"; text: string;
  suggestion?: Suggestion | null;
  discovered?: Discovered | null;
  relevant?: Record<string, string>;   // item id ("tool" or "METHOD /path") → why it's relevant
  showAll?: boolean;                    // UI: browse-all expanded
  error?: boolean; system?: boolean;
};

// Stable id for a discovered item — matches what the LLM keys "relevant" on.
const itemId = (d: Discovered, it: DiscItem): string =>
  d.kind === "mcp" ? (it.name ?? "") : `${it.method ?? ""} ${it.path ?? ""}`.trim();

// Build a connector config from a single discovered item — no extra LLM call, so
// the operator can pick ANY item, not just the assistant's top suggestion.
const configForItem = (d: Discovered, it: DiscItem): Suggestion => {
  if (d.kind === "mcp") {
    const qa = (it.args ?? []).find((a) => /^(query|question|text|prompt|q|search)$/i.test(a)) ?? (it.args ?? [])[0];
    return { kind: "mcp", name: it.name ?? "mcp_tool", description: it.description ?? "",
      config: { server: d.base_url ?? "", tool: it.name ?? "", ...(qa ? { query_arg: qa } : {}) } };
  }
  const leaf = (it.path ?? "").split("/").filter(Boolean).pop() ?? "endpoint";
  return { kind: "http", name: leaf.replace(/[^a-z0-9_]+/gi, "_").toLowerCase(), description: it.summary ?? "",
    config: { url: (d.base_url ?? "") + (it.path ?? ""), method: it.method ?? "GET" } };
};

type ConnectorRow = {
  name: string; kind: string; description: string; config: Record<string, unknown>;
  enabled: boolean; updated_at: string; sop_references: number; stats_window_days: number;
  stats: { fetches: number; errors: number; consumed: number; p50_ms: number; p95_ms: number; last_used: string | null };
};
type TestResult = { ok: boolean; latency_ms: number; summary: string; payload_excerpt?: string | null; error?: string };

const KIND_TONE: Record<string, string> = { mcp: "accent", rag: "comm", http: "warn", mock: "" };
const CONFIG_HINTS: Record<string, string> = {
  mcp: '{"server": "https://kg.example.com/mcp", "tool": "query_knowledge_graph", "query_arg": "query", "auth_secret": "kr_api_key", "auth_header": "X-API-Key"}',
  rag: '{"corpus": "policies", "top_k": 3}',
  http: '{"url": "https://rag.internal/search", "method": "POST", "query_field": "query", "result_path": "results", "auth_secret": "rag_key", "auth_header": "Authorization"}',
  mock: "{}",
};

export default function ConnectorsView() {
  const [rows, setRows] = useState<ConnectorRow[]>([]);
  const [secrets, setSecrets] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("mcp");
  const [description, setDescription] = useState("");
  const [configText, setConfigText] = useState(CONFIG_HINTS.mcp);
  const [enabled, setEnabled] = useState(true);
  const [note, setNote] = useState("");
  const [testQuery, setTestQuery] = useState("connectivity test — say hello");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");

  // ---- Connector assistant: discover an API and suggest a connector (chat + history) ----
  const [chatOpen, setChatOpen] = useState(false);
  const [ask, setAsk] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);

  const sendMessage = async () => {
    const q = ask.trim();
    if (!q || askBusy) return;
    setAsk("");
    const history = messages.filter((m) => !m.system).map((m) => ({ role: m.role, content: m.text }));
    setMessages((ms) => [...ms, { role: "user", text: q }]);
    setAskBusy(true);
    try {
      const r = await api<{ reply?: string; suggestion?: Suggestion | null; discovered?: Discovered; relevant?: Record<string, string>; error?: string }>(
        "POST", "/connectors/suggest", { instruction: q, history });
      if (r.error) { setMessages((ms) => [...ms, { role: "assistant", text: r.error!, error: true }]); return; }
      setMessages((ms) => [...ms, { role: "assistant", text: r.reply || "", suggestion: r.suggestion, discovered: r.discovered, relevant: r.relevant }]);
    } catch (e: unknown) {
      const m = String((e as { message?: string })?.message ?? e);
      setMessages((ms) => [...ms, { role: "assistant", text: m.includes("Not Found") ? "Assistant endpoint not found — restart the backend." : `Assistant failed: ${m}`, error: true }]);
    } finally { setAskBusy(false); }
  };

  const applyConfig = (s: Suggestion) => {
    if (s.name) setName(s.name);
    setKind(s.kind);
    setDescription(s.description || "");
    setConfigText(JSON.stringify(s.config, null, 2));
    setEnabled(true);
    setTestResult(null);
    const secret = (s.config as { auth_secret?: string }).auth_secret;
    setNote(`Loaded “${s.name}” into the editor from the assistant — review, then Save.`
      + (secret ? ` It references secret “${secret}” — add its value under Tenant secrets first.` : ""));
    setMessages((ms) => ms.concat([{ role: "assistant", system: true, text: `Loaded “${s.name}” into the editor — review and Save.` }]));
  };
  const applySuggestion = (idx: number) => { const s = messages[idx]?.suggestion; if (s) applyConfig(s); };
  const toggleShowAll = (idx: number) =>
    setMessages((ms) => ms.map((m, i) => (i === idx ? { ...m, showAll: !m.showAll } : m)));

  const refresh = useCallback(async () => {
    setRows(await api<ConnectorRow[]>("GET", "/connectors"));
    api<Array<{ name: string }>>("GET", "/secrets").then((s) => setSecrets(s.map((x) => x.name))).catch(() => {});
  }, []);
  useEffect(() => {
    refresh().catch((e) => setNote(String(e)));
  }, [refresh]);

  const open = (c: ConnectorRow) => {
    setName(c.name);
    setKind(c.kind);
    setDescription(c.description);
    setConfigText(JSON.stringify(c.config, null, 2));
    setEnabled(c.enabled);
    setTestResult(null);
    setNote("");
  };

  const save = async () => {
    setNote("");
    let config: Record<string, unknown>;
    try {
      config = configText.trim() ? JSON.parse(configText) : {};
    } catch (e) {
      setNote(`config is not valid JSON: ${e}`);
      return;
    }
    try {
      await api("PUT", `/connectors/${encodeURIComponent(name)}`, { kind, description, config, enabled });
      setNote("saved — running sessions keep the config they resolved; new fetches use this one");
      await refresh();
    } catch (e) {
      setNote(String(e));
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await api<TestResult>("POST", `/connectors/${encodeURIComponent(name)}/test`, { query: testQuery }));
    } catch (e) {
      setTestResult({ ok: false, latency_ms: 0, summary: "", error: String(e) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Studio</div>
          <h1>Connectors</h1>
          <p>
            The retrieval systems behind background prefetch — MCP servers, RAG/HTTP endpoints, managed corpora.
            SOP stages bind by name (<code>config.connector</code>); swap the system here without republishing any SOP.
          </p>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <div className="chead">
            <h3>Registry &amp; health</h3>
            <span className="sub num">{rows.length} connectors · last {rows[0]?.stats_window_days ?? 7}d</span>
          </div>
          <div className="cbody" style={{ padding: 0 }}>
            {rows.length === 0 ? (
              <div className="empty">No connectors yet — configure the first on the right, then reference it from an SOP stage's data dependency.</div>
            ) : (
              <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 460 }}>
                <table className="table">
                  <thead>
                    <tr><th>Name</th><th>Kind</th><th>Health (7d)</th><th>SOPs</th><th></th></tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => {
                      const errRate = c.stats.fetches ? c.stats.errors / c.stats.fetches : 0;
                      return (
                        <tr key={c.name} onClick={() => open(c)} style={{ cursor: "pointer", opacity: c.enabled ? 1 : 0.55 }}>
                          <td className="mono" style={{ fontSize: 12.5 }}>
                            {c.name}
                            {!c.enabled && <span className="st warn" style={{ marginLeft: 6 }}>disabled</span>}
                          </td>
                          <td><span className={"chip " + (KIND_TONE[c.kind] ?? "")}>{c.kind}</span></td>
                          <td>
                            {c.stats.fetches === 0 ? (
                              <span style={{ color: "var(--muted)", fontSize: 12 }}>no traffic</span>
                            ) : (
                              <span style={{ display: "inline-flex", gap: 5, flexWrap: "wrap" }}>
                                <span className="chip"><span className="cd" />{c.stats.fetches} fetches</span>
                                <span className={"chip " + (errRate > 0.05 ? "crit" : errRate > 0 ? "warn" : "good")}>
                                  <span className="cd" />{Math.round(errRate * 100)}% err
                                </span>
                                <span className="chip"><span className="cd" />p95 {c.stats.p95_ms} ms</span>
                              </span>
                            )}
                          </td>
                          <td className="mono num">{c.sop_references || "—"}</td>
                          <td style={{ width: 34 }}>
                            <button
                              className="btn ghost sm"
                              title={`Delete ${c.name}`}
                              onClick={async (e) => {
                                e.stopPropagation();
                                if (!window.confirm(`Delete connector “${c.name}”? Dependencies binding it will fail their fetches (audited, live path degrades gracefully).`)) return;
                                await api("DELETE", `/connectors/${encodeURIComponent(c.name)}`);
                                if (name === c.name) setName("");
                                await refresh();
                              }}
                            >
                              <Trash2 size={14} />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="card">
            <div className="chead"><h3>Editor</h3></div>
            <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", gap: 8 }}>
                <input className="qinput mono" placeholder="name, e.g. emr or kb" value={name} onChange={(e) => setName(e.target.value)} />
                <select
                  className="qinput" style={{ flex: "none" }} value={kind}
                  onChange={(e) => {
                    setKind(e.target.value);
                    if (!configText.trim() || Object.values(CONFIG_HINTS).includes(configText)) setConfigText(CONFIG_HINTS[e.target.value] ?? "{}");
                  }}
                >
                  <option value="mcp">mcp — tool on an MCP server</option>
                  <option value="rag">rag — managed pgvector corpus</option>
                  <option value="http">http — RAG/search/tool endpoint</option>
                  <option value="mock">mock — stand-in for development</option>
                </select>
              </div>
              <input className="qinput" placeholder="description (what system is this?)" value={description} onChange={(e) => setDescription(e.target.value)} />
              <textarea className="area mono" rows={7} style={{ fontSize: 12 }} value={configText} onChange={(e) => setConfigText(e.target.value)} spellCheck={false} />
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
                  <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> enabled
                </label>
                <button className="btn primary" disabled={!name} onClick={save}><Save /> Save</button>
                <span style={{ fontSize: 11.5, color: "var(--muted)" }}>credentials go in tenant secrets — reference by <code>auth_secret</code> name</span>
              </div>
              {note && <p style={{ margin: 0, color: "var(--muted)", fontSize: 12.5 }}>{note}</p>}
            </div>
          </div>

          <div className="card">
            <div className="chead">
              <h3>Live test</h3>
              {name ? <span className="sub mono">{name}</span> : <span className="sub">save a connector first</span>}
            </div>
            <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", gap: 6 }}>
                <input className="qinput" value={testQuery} onChange={(e) => setTestQuery(e.target.value)} placeholder="test query" />
                <button className="btn" disabled={!name || testing} onClick={runTest}>
                  <Zap /> {testing ? "Testing…" : "Test now"}
                </button>
              </div>
              {testResult && (
                <div style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px" }}>
                  <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                    <span className={"chip " + (testResult.ok ? "good" : "crit")}><span className="cd" />{testResult.ok ? "reachable" : "failed"}</span>
                    <span className="chip"><span className="cd" />{testResult.latency_ms} ms</span>
                  </div>
                  {testResult.error && <div style={{ fontSize: 12.5, color: "var(--crit)" }}>{testResult.error}</div>}
                  {testResult.summary && <div style={{ fontSize: 12.5, marginBottom: 4 }}><b>summary:</b> {testResult.summary}</div>}
                  {testResult.payload_excerpt && (
                    <pre style={{ margin: 0, fontSize: 11.5, whiteSpace: "pre-wrap", maxHeight: 160, overflow: "auto", color: "var(--text2)" }}>{testResult.payload_excerpt}</pre>
                  )}
                </div>
              )}
              <p style={{ margin: 0, fontSize: 11.5, color: "var(--muted)" }}>
                <Plug size={12} style={{ verticalAlign: -2 }} /> fires ONE real fetch through the production fetcher — nothing pools, nothing audits.
              </p>
            </div>
          </div>

          <div className="card">
            <div className="chead">
              <h3>Tenant secrets</h3>
              <span className="sub num">{secrets.length} stored (names only — values never readable)</span>
            </div>
            <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {secrets.length > 0 && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {secrets.map((s) => (
                    <span key={s} className="chip"><KeyRound size={11} /> {s}</span>
                  ))}
                </div>
              )}
              <div style={{ display: "flex", gap: 6 }}>
                <input className="qinput mono" placeholder="secret name" value={secretName} onChange={(e) => setSecretName(e.target.value)} />
                <input className="qinput mono" type="password" placeholder="value (encrypted at rest)" value={secretValue} onChange={(e) => setSecretValue(e.target.value)} />
                <button
                  className="btn" disabled={!secretName || !secretValue}
                  onClick={async () => {
                    await api("PUT", "/secrets", { name: secretName, value: secretValue });
                    setSecretName(""); setSecretValue("");
                    await refresh();
                  }}
                >
                  <Save /> Store
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ---- Connector assistant: floating, persistent, discovers an API and suggests a connector ---- */}
      {!chatOpen && (
        <button className="btn primary" onClick={() => setChatOpen(true)}
          style={{ position: "fixed", right: 20, bottom: 20, zIndex: 50, borderRadius: 999, boxShadow: "0 6px 24px rgba(0,0,0,.35)" }}>
          <Sparkles size={15} /> Connector assistant{messages.filter((m) => m.role === "user").length ? ` (${messages.filter((m) => m.role === "user").length})` : ""}
        </button>
      )}
      {chatOpen && (
        <div style={{ position: "fixed", right: 20, bottom: 20, zIndex: 50, width: 420, maxWidth: "calc(100vw - 40px)",
          height: "min(600px, 82vh)", display: "flex", flexDirection: "column",
          background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 12, boxShadow: "0 10px 40px rgba(0,0,0,.3)" }}>
          <div className="chead" style={{ borderBottom: "1px solid var(--line)", padding: "10px 12px", display: "flex", alignItems: "center" }}>
            <Sparkles size={15} style={{ marginRight: 6 }} /><span>Connector assistant</span>
            <span className="sub" style={{ marginLeft: 6, fontSize: 11 }}>· finds the right API endpoint</span>
            <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              {messages.length > 0 && <button className="btn ghost sm" title="Clear the conversation" onClick={() => setMessages([])}>Clear</button>}
              <button className="btn ghost sm" onClick={() => setChatOpen(false)}><X size={14} /></button>
            </span>
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
            {messages.length === 0 && (
              <div className="sub" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
                Paste an API URL and say what you need — I'll probe it for <b>MCP tools</b> or an <b>OpenAPI/FastAPI</b> spec
                and suggest a connector. E.g.:<br />
                <span className="mono" style={{ fontSize: 11.5 }}>“http://10.0.0.80:9621 — query our knowledge base for relevant passages”</span>
              </div>
            )}
            {messages.map((m, i) => {
              if (m.role === "user") return (
                <div key={i} style={{ alignSelf: "flex-end", maxWidth: "88%", background: "var(--accent-soft, rgba(59,110,245,.12))",
                  borderRadius: "10px 10px 2px 10px", padding: "7px 10px", fontSize: 12.5, wordBreak: "break-word" }}>{m.text}</div>
              );
              if (m.system) return (
                <div key={i} className="sub" style={{ alignSelf: "flex-start", fontSize: 11.5, color: "var(--muted)", padding: "2px 4px" }}>✓ {m.text}</div>
              );
              return (
                <div key={i} style={{ alignSelf: "flex-start", maxWidth: "92%",
                  background: m.error ? "var(--crit-dim, rgba(209,52,56,.1))" : "var(--panel2, rgba(127,127,127,.1))",
                  borderRadius: "10px 10px 10px 2px", padding: "8px 10px", fontSize: 12.5,
                  color: m.error ? "var(--crit)" : "var(--text2)", lineHeight: 1.45, wordBreak: "break-word" }}>
                  <span>{m.text}</span>
                  {m.suggestion && (
                    <div style={{ marginTop: 8, border: "1px solid var(--accent, #3b6ef5)", borderRadius: 8, padding: "8px 10px", background: "var(--surface)" }}>
                      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 4 }}>
                        <span className="chip good" style={{ fontSize: 10.5 }}>★ recommended</span>
                        <span className="chip accent" style={{ fontSize: 11 }}>{m.suggestion.kind}</span>
                        <b className="mono" style={{ fontSize: 12 }}>{m.suggestion.name}</b>
                      </div>
                      <pre className="mono" style={{ margin: "0 0 6px", fontSize: 11, whiteSpace: "pre-wrap", color: "var(--text2)", maxHeight: 150, overflow: "auto" }}>{JSON.stringify(m.suggestion.config, null, 2)}</pre>
                      <button className="btn sm primary" onClick={() => applySuggestion(i)}>Apply to editor</button>
                    </div>
                  )}
                  {m.discovered && m.discovered.kind !== "none" && (() => {
                    const d = m.discovered!; const rel = m.relevant ?? {}; const items = d.items ?? [];
                    const relItems = items.filter((it) => itemId(d, it) in rel);
                    const shown = m.showAll ? items : relItems;
                    return (
                      <div style={{ marginTop: 8 }}>
                        <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
                          <span className="chip good" style={{ fontSize: 11 }}><span className="cd" />{d.count} {d.kind} item{d.count === 1 ? "" : "s"} found</span>
                          {items.length > 0 && items.length !== shown.length && (
                            <button className="btn ghost sm" onClick={() => toggleShowAll(i)}>Browse all {d.count}</button>
                          )}
                          {m.showAll && relItems.length > 0 && (
                            <button className="btn ghost sm" onClick={() => toggleShowAll(i)}>Show relevant only</button>
                          )}
                        </div>
                        {shown.length === 0 && (
                          <div className="sub" style={{ fontSize: 11.5 }}>
                            {relItems.length === 0 ? "None flagged as clearly relevant — " : ""}Browse all {d.count} to compare every tool.
                          </div>
                        )}
                        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                          {shown.map((it, j) => {
                            const id = itemId(d, it); const why = rel[id];
                            const label = d.kind === "mcp" ? it.name : `${it.method} ${it.path}`;
                            const desc = d.kind === "mcp" ? it.description : it.summary;
                            return (
                              <div key={j} style={{ border: "1px solid var(--line)", borderLeft: why ? "2px solid var(--warn)" : "1px solid var(--line)", borderRadius: 8, padding: "7px 9px", background: "var(--surface)" }}>
                                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                                  {why && <span title="flagged relevant to your request" style={{ color: "var(--warn)" }}>★</span>}
                                  <b className="mono" style={{ fontSize: 11.5, wordBreak: "break-all" }}>{label}</b>
                                  {d.kind === "mcp" && (it.args?.length ?? 0) > 0 && <span className="sub mono" style={{ fontSize: 10.5 }}>({it.args!.join(", ")})</span>}
                                  <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => applyConfig(configForItem(d, it))}>Use this</button>
                                </div>
                                {why && <div style={{ fontSize: 11.5, color: "var(--warn)", marginTop: 2 }}>→ {why}</div>}
                                {desc && <div className="sub" style={{ fontSize: 11.5, marginTop: 2, lineHeight: 1.4 }}>{desc}</div>}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })()}
                </div>
              );
            })}
            {askBusy && <div className="sub" style={{ fontSize: 12 }}>Probing &amp; drafting…</div>}
          </div>
          <div style={{ display: "flex", gap: 6, padding: "10px 12px", borderTop: "1px solid var(--line)" }}>
            <input className="qinput" style={{ flex: 1 }} placeholder="API URL + what you need it to do…" value={ask}
              onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => e.key === "Enter" && sendMessage()} />
            <button className="btn sm primary" onClick={sendMessage} disabled={askBusy || !ask.trim()}><Send size={14} /></button>
          </div>
        </div>
      )}
    </div>
  );
}
