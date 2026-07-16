import { ChevronDown, ChevronRight, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type TraceSummary = { sops: Array<{ sop_id: string; sop_name: string; total: number; by_outcome: Record<string, number> }> };
type Facets = { actions: Array<{ name: string; count: number }>; outcomes: Array<{ name: string; count: number }> };
type Trace = {
  id: string;
  sop_id: string;
  session_id: string;
  turn_index: number;
  cohort: string;
  mood: string;
  action: string;
  immediate_state: string;
  terminal_outcome: string | null;
  terminal_reward: number | null;
  turn_distance_to_terminal: number | null;
  response_text: string;
  has_embedding: boolean;
  created_at: string;
};

const OUTCOME_TONE: Record<string, string> = { success: "good", failure: "crit", abandoned: "warn", in_progress: "" };
const PAGE = 50;

export default function TracesView() {
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [items, setItems] = useState<Trace[]>([]);
  const [total, setTotal] = useState(0);
  const [sopId, setSopId] = useState("");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [q, setQ] = useState("");
  const [qDraft, setQDraft] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(
    async (offset: number) => {
      setErr("");
      const p = new URLSearchParams();
      if (sopId) p.set("sop_id", sopId);
      if (action) p.set("action", action);
      if (outcome) p.set("outcome", outcome);
      if (q) p.set("q", q);
      p.set("limit", String(PAGE));
      p.set("offset", String(offset));
      try {
        const r = await api<{ total: number; items: Trace[] }>("GET", `/traces?${p}`);
        setTotal(r.total);
        setItems((prev) => (offset === 0 ? r.items : [...prev, ...r.items]));
      } catch (e) {
        setErr(String(e));
      }
    },
    [sopId, action, outcome, q]
  );

  useEffect(() => {
    api<TraceSummary>("GET", "/traces/summary").then(setSummary).catch((e) => setErr(String(e)));
  }, []);
  useEffect(() => {
    const p = sopId ? `?sop_id=${sopId}` : "";
    api<Facets>("GET", `/traces/facets${p}`).then(setFacets).catch(() => {});
  }, [sopId]);
  useEffect(() => {
    load(0);
  }, [load]);

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Precedent traces</h1>
          <p>
            What the predictor has learned from — every real turn distilled to (situation → action → outcome). These
            rows fuel prediction, prefetch and pre-drafted replies for their tenant.
          </p>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => load(0)}>
            <RefreshCw /> Refresh
          </button>
        </div>
      </div>
      {err && (
        <span className="chip crit" style={{ whiteSpace: "normal" }}>
          <span className="cd" />
          {err}
        </span>
      )}

      {summary && summary.sops.length > 0 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "4px 0 14px" }}>
          {summary.sops.map((s) => (
            <button
              key={s.sop_id}
              className={"card" + (sopId === s.sop_id ? " sel" : "")}
              style={{ cursor: "pointer", textAlign: "left", padding: 0, border: sopId === s.sop_id ? "1px solid var(--accent)" : undefined }}
              onClick={() => setSopId(sopId === s.sop_id ? "" : s.sop_id)}
            >
              <div className="cbody" style={{ padding: "10px 14px" }}>
                <div style={{ fontWeight: 700, fontSize: 13 }}>{s.sop_name}</div>
                <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                  <span className="chip">
                    <span className="cd" />
                    {s.total} traces
                  </span>
                  {Object.entries(s.by_outcome).map(([k, v]) => (
                    <span key={k} className={"chip " + (OUTCOME_TONE[k] ?? "")}>
                      <span className="cd" />
                      {k} <span className="num">{v}</span>
                    </span>
                  ))}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <select className="qinput" style={{ flex: "none" }} value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">all actions</option>
          {facets?.actions.map((a) => (
            <option key={a.name} value={a.name}>
              {a.name} ({a.count})
            </option>
          ))}
        </select>
        <select className="qinput" style={{ flex: "none" }} value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          <option value="">all outcomes</option>
          {facets?.outcomes.map((o) => (
            <option key={o.name} value={o.name}>
              {o.name} ({o.count})
            </option>
          ))}
        </select>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            className="qinput"
            placeholder="search response text…"
            value={qDraft}
            onChange={(e) => setQDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && setQ(qDraft)}
          />
          <button className="btn" onClick={() => setQ(qDraft)}>
            <Search /> Search
          </button>
        </div>
        <span style={{ alignSelf: "center", color: "var(--muted)", fontSize: 12.5 }}>
          {total} trace{total === 1 ? "" : "s"} match
        </span>
      </div>

      <div className="tablewrap">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 26 }} />
                <th>when</th>
                <th>session · turn</th>
                <th>action</th>
                <th>state after</th>
                <th>cohort / mood</th>
                <th>outcome</th>
                <th>reward</th>
                <th>→ terminal</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <>
                  <tr key={t.id} style={{ cursor: "pointer" }} onClick={() => setOpen(open === t.id ? null : t.id)}>
                    <td>{open === t.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                    <td className="num" style={{ fontSize: 12 }}>{new Date(t.created_at).toLocaleString()}</td>
                    <td className="num" style={{ fontSize: 12 }}>
                      {t.session_id.slice(0, 8)} · {t.turn_index}
                    </td>
                    <td>
                      <span className="chip accent">
                        <span className="cd" />
                        {t.action}
                      </span>
                    </td>
                    <td style={{ fontSize: 12.5 }}>{t.immediate_state || "—"}</td>
                    <td style={{ fontSize: 12.5, color: "var(--muted)" }}>
                      {[t.cohort, t.mood].filter(Boolean).join(" / ") || "—"}
                    </td>
                    <td>
                      <span className={"chip " + (OUTCOME_TONE[t.terminal_outcome ?? "in_progress"] ?? "")}>
                        <span className="cd" />
                        {t.terminal_outcome ?? "in_progress"}
                      </span>
                    </td>
                    <td className="num">{t.terminal_reward === null ? "—" : t.terminal_reward.toFixed(2)}</td>
                    <td className="num">{t.turn_distance_to_terminal ?? "—"}</td>
                  </tr>
                  {open === t.id && (
                    <tr key={t.id + "x"}>
                      <td />
                      <td colSpan={8} style={{ padding: "6px 10px 14px" }}>
                        <div style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, marginBottom: 4 }}>
                          AGENT RESPONSE AT THIS TURN {t.has_embedding ? "· situation embedded" : "· no embedding"}
                        </div>
                        <div style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.55 }}>
                          {t.response_text || <i style={{ color: "var(--muted)" }}>(no response text recorded)</i>}
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={9} style={{ textAlign: "center", color: "var(--muted)", padding: 24 }}>
                    No traces match — run some conversations first (Playground or the API) and finished turns land
                    here.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
      </div>
      {items.length < total && (
        <div style={{ marginTop: 10, textAlign: "center" }}>
          <button className="btn" onClick={() => load(items.length)}>
            Load {Math.min(PAGE, total - items.length)} more
          </button>
        </div>
      )}
    </div>
  );
}
