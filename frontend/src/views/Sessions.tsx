import { RefreshCw } from "lucide-react";
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

const OUTCOME_TONE: Record<string, string> = { success: "good", failure: "crit", abandoned: "warn" };

export default function SessionsView() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [pool, setPool] = useState<PoolItem[] | null>(null);

  const refresh = useCallback(async () => setSessions(await api<Session[]>("GET", "/sessions")), []);
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const openPool = async (id: string) => {
    setSelected(id);
    const snap = await api("GET", `/sessions/${id}/pool`);
    setPool(snap.items);
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
                    <tr><th>Session</th><th>Channel</th><th>Status</th><th>Outcome</th><th>Started</th></tr>
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
            {pool && <span className="sub num">{pool.length} live items</span>}
          </div>
          <div className="cbody" style={{ padding: pool && pool.length ? 0 : undefined }}>
            {!pool ? (
              <div className="empty">Select a session to inspect its pool.</div>
            ) : pool.length === 0 ? (
              <div className="empty">Pool is empty (session ended or nothing pre-staged yet).</div>
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
    </div>
  );
}
