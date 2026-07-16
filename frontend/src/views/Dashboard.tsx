import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";

type Summary = {
  window_days: number;
  sessions: { total: number; by_outcome: Record<string, number> };
  data: {
    fetches: number;
    speculative_hit_rate: number | null;
    live_fallback_rate: number | null;
    latency_hidden_s_per_session: number;
  };
  instructions: {
    drafts: number;
    drafts_served: number;
    turn_hits: number;
    hit_rate_vs_eligible_turns: number | null;
    draft_efficiency: number | null;
  };
  selection: { turns_with_rerank: number; pick_rate: number | null; rerank_ms_p50: number; rerank_ms_p95: number };
  supervisor_lag_ms: number | null;
};

const OUTCOME_TONE: Record<string, string> = {
  success: "good", failure: "crit", abandoned: "warn", in_progress: "accent", no_outcome: "",
};

function pctOrDash(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: string }) {
  return (
    <div className="card">
      {tone && <span className={`stripe ${tone}`} />}
      <div className="cbody">
        <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>{label}</div>
        <div className="num" style={{ fontSize: 29, fontWeight: 700, letterSpacing: "-1px", margin: "2px 0" }}>{value}</div>
        <div style={{ fontSize: 12, color: "var(--muted)" }}>{sub}</div>
      </div>
    </div>
  );
}

export default function DashboardView() {
  const [days, setDays] = useState(7);
  const [m, setM] = useState<Summary | null>(null);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    setErr("");
    try {
      setM(await api<Summary>("GET", `/metrics/summary?days=${days}`));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404)
        setErr("The metrics endpoint isn't deployed on this server yet — it activates on the next backend restart.");
      else setErr(String(e));
    }
  }, [days]);
  useEffect(() => {
    refresh();
  }, [refresh]);

  const hitTone = (v: number | null, good: number, warn: number) =>
    v === null ? undefined : v >= good ? "good" : v >= warn ? "warn" : "crit";

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Dashboard</h1>
          <p>The four SLIs, computed from the audit trail — the same numbers the architecture doc commits to.</p>
        </div>
        <div className="actions">
          <select className="qinput" style={{ flex: "none" }} value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={1}>last 24h</option>
            <option value={7}>last 7 days</option>
            <option value={30}>last 30 days</option>
          </select>
          <button className="btn" onClick={refresh}><RefreshCw /> Refresh</button>
        </div>
      </div>
      {err && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{err}</span>}
      {m && (
        <>
          <div className="kpis">
            <Kpi
              label="PRE-STAGED AVAILABILITY"
              value={pctOrDash(m.data.speculative_hit_rate)}
              sub={`of consumed lookups came from the pool · target ≥ 70%`}
              tone={hitTone(m.data.speculative_hit_rate, 0.7, 0.6)}
            />
            <Kpi
              label="CALLER-FELT WAITS"
              value={pctOrDash(m.data.live_fallback_rate)}
              sub="live blocking lookups · target < 10%"
              tone={m.data.live_fallback_rate === null ? undefined : m.data.live_fallback_rate <= 0.1 ? "good" : "crit"}
            />
            <Kpi
              label="INSTRUCTION HITS"
              value={pctOrDash(m.instructions.hit_rate_vs_eligible_turns)}
              sub={`${m.instructions.turn_hits} turn hits · ${m.instructions.drafts_served}/${m.instructions.drafts} drafts served · claim gate ≥ 70%`}
              tone={hitTone(m.instructions.hit_rate_vs_eligible_turns, 0.7, 0.4)}
            />
            <Kpi
              label="WAITING REMOVED"
              value={`${m.data.latency_hidden_s_per_session}s`}
              sub="external-lookup time hidden per session (mean)"
              tone="accent"
            />
          </div>
          <div className="kpis">
            <Kpi
              label="CONTEXT SELECTION"
              value={`${m.selection.rerank_ms_p50} ms`}
              sub={`p95 ${m.selection.rerank_ms_p95} ms · picks on ${pctOrDash(m.selection.pick_rate)} of turns`}
            />
            <Kpi
              label="SUPERVISOR LAG"
              value={m.supervisor_lag_ms === null ? "—" : `${m.supervisor_lag_ms} ms`}
              sub="oldest unprocessed turn event (early warning)"
              tone={m.supervisor_lag_ms !== null && m.supervisor_lag_ms > 5000 ? "warn" : undefined}
            />
            <Kpi label="DATA FETCHES" value={String(m.data.fetches)} sub={`across ${m.sessions.total} sessions in window`} />
            <div className="card">
              <div className="cbody">
                <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>SESSIONS BY OUTCOME</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10 }}>
                  {Object.entries(m.sessions.by_outcome).map(([k, v]) => (
                    <span key={k} className={"chip " + (OUTCOME_TONE[k] ?? "")}>
                      <span className="cd" />{k} <span className="num">{v}</span>
                    </span>
                  ))}
                  {m.sessions.total === 0 && <span style={{ color: "var(--muted)", fontSize: 12.5 }}>no sessions in window</span>}
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
