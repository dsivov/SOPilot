import { CircleStop, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type Session = {
  session_id: string; sop_id: string; sop_version: number; channel: string;
  status: string; terminal_outcome: string | null; started_at: string;
};
type PoolItem = {
  item_id: string; kind: string; dependency_name: string; source_action: string;
  payload_summary: string; confidence: number; predictor_source: string;
  predicted_user_state: string | null; fetched_at: string; expires_at: string;
};
type FetchRow = {
  kind: string; dependency_name: string; action_name: string; predictor_source: string;
  speculative: boolean; consumed: boolean; wasted: boolean; confidence: number;
  fetch_duration_ms: number; issued_at_turn: number; consumed_at_turn: number | null;
  payload_summary: string; error: boolean;
};

const OUTCOME_TONE: Record<string, string> = { success: "good", failure: "crit", abandoned: "warn" };

export default function SessionsView() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [pool, setPool] = useState<PoolItem[] | null>(null);
  const [audit, setAudit] = useState<FetchRow[] | null>(null);
  const [auditNote, setAuditNote] = useState("");

  const refresh = useCallback(async () => setSessions(await api<Session[]>("GET", "/sessions")), []);
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const openPool = async (id: string) => {
    setSelected(id);
    const snap = await api("GET", `/sessions/${id}/pool`);
    setPool(snap.items);
    setAudit(null);
    setAuditNote("");
    try {
      const a = await api("GET", `/sessions/${id}/fetches`);
      setAudit(a.fetches);
    } catch {
      setAuditNote("audit endpoint activates on the next backend restart");
    }
  };

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Sessions</h1>
          <p>Recent conversations and the live pool X-ray (what the supervisor pre-staged).</p>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => refresh()}>
            <RefreshCw /> Refresh
          </button>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <div className="chead">
            <h3>Recent sessions</h3>
            <span className="sub num">{sessions.length}</span>
          </div>
          <div className="cbody" style={{ padding: 0 }}>
            {sessions.length === 0 ? (
              <div className="empty">No sessions in this project yet.</div>
            ) : (
              <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 420 }}>
                <table className="table">
                  <thead>
                    <tr><th>Session</th><th>Channel</th><th>Status</th><th>Outcome</th><th>Started</th><th></th></tr>
                  </thead>
                  <tbody>
                    {sessions.map((s) => (
                      <tr key={s.session_id} className={selected === s.session_id ? "sel" : ""} onClick={() => openPool(s.session_id)} style={{ cursor: "pointer" }}>
                        <td className="mono" style={{ fontSize: 12 }}>{s.session_id.slice(0, 12)}…</td>
                        <td>{s.channel}</td>
                        <td><span className={"st " + (s.status === "active" ? "accent" : "")}>{s.status}</span></td>
                        <td>
                          {s.terminal_outcome ? (
                            <span className={"st " + (OUTCOME_TONE[s.terminal_outcome] ?? "")}>{s.terminal_outcome}</span>
                          ) : (
                            <span style={{ color: "var(--muted)" }}>—</span>
                          )}
                        </td>
                        <td style={{ color: "var(--muted)", fontSize: 12, whiteSpace: "nowrap" }}>{s.started_at.slice(0, 16).replace("T", " ")}</td>
                        <td style={{ width: 34 }}>
                          {s.status === "active" && (
                            <button
                              className="btn ghost sm"
                              title="End this session (marks it abandoned)"
                              onClick={async (e) => {
                                e.stopPropagation();
                                await api("POST", `/sessions/${s.session_id}/outcome`, { outcome: "abandoned" }).catch(() => undefined);
                                await api("POST", `/sessions/${s.session_id}/end`, {});
                                await refresh();
                              }}
                            >
                              <CircleStop size={14} />
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
        <div className="card">
          <div className="chead">
            <h3>Pool X-ray</h3>
            {pool && <span className="sub num">{pool.length} live · {audit ? `${audit.length} audited` : ""}</span>}
          </div>
          <div className="cbody" style={{ padding: pool && pool.length ? 0 : undefined }}>
            {!pool ? (
              <div className="empty">Select a session to inspect its pool.</div>
            ) : pool.length === 0 ? (
              <div className="empty">
                No LIVE pool items — the pool is cleared when a session ends and items expire by TTL.
                The permanent prefetch record is below.
              </div>
            ) : (
              <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 420 }}>
                <table className="table">
                  <thead>
                    <tr><th>Kind</th><th>Dependency</th><th>Summary</th><th>Source</th></tr>
                  </thead>
                  <tbody>
                    {pool.map((p) => (
                      <tr key={p.item_id}>
                        <td><span className={"chip " + (p.kind === "instruction" ? "comm" : "accent")}>{p.kind}</span></td>
                        <td className="mono" style={{ fontSize: 12 }}>{p.dependency_name}</td>
                        <td style={{ fontSize: 12.5, color: "var(--text2)" }}>{p.payload_summary.slice(0, 90)}</td>
                        <td><span className="chip">{p.predictor_source}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {selected && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="chead">
            <h3>Prefetch audit — the permanent record</h3>
            {audit && (
              <span className="sub num">
                {audit.filter((f) => f.consumed).length} served · {audit.filter((f) => f.wasted).length} unused ·{" "}
                {audit.length} total
              </span>
            )}
          </div>
          <div className="cbody" style={{ padding: audit && audit.length ? 0 : undefined }}>
            {auditNote ? (
              <div className="empty">{auditNote}</div>
            ) : !audit ? (
              <div className="spin" />
            ) : audit.length === 0 ? (
              <div className="empty">No fetches were made for this session.</div>
            ) : (
              <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 340 }}>
                <table className="table">
                  <thead>
                    <tr><th>Kind</th><th>Dependency</th><th>Source</th><th>Fate</th><th>ms</th><th>Turn</th><th>Summary</th></tr>
                  </thead>
                  <tbody>
                    {audit.map((f, i) => (
                      <tr key={i}>
                        <td><span className={"chip " + (f.kind === "instruction" ? "comm" : "accent")}>{f.kind}</span></td>
                        <td className="mono" style={{ fontSize: 12 }}>{f.dependency_name}</td>
                        <td><span className="chip">{f.predictor_source}</span></td>
                        <td>
                          {f.error ? (
                            <span className="st crit">error</span>
                          ) : f.consumed ? (
                            <span className="st good">served{f.consumed_at_turn !== null ? ` @${f.consumed_at_turn}` : ""}</span>
                          ) : f.wasted ? (
                            <span className="st warn">unused</span>
                          ) : (
                            <span className="st accent">pending</span>
                          )}
                        </td>
                        <td className="mono num">{f.fetch_duration_ms}</td>
                        <td className="mono num">{f.issued_at_turn}</td>
                        <td style={{ fontSize: 12, color: "var(--text2)" }}>{f.payload_summary.slice(0, 60)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
