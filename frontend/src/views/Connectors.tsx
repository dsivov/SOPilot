// Connector registry (D-10): configure, monitor, and live-test the retrieval
// systems behind background prefetch. Connection details live here; SOP stages
// bind by name via data_dependencies[].config.connector.
import { KeyRound, Plug, Save, Trash2, Zap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

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
    </div>
  );
}
