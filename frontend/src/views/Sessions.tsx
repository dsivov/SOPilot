import { CircleStop, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import GraphView from "./GraphView";

type Session = {
  session_id: string; sop_id: string; sop_version: number; channel: string;
  status: string; terminal_outcome: string | null; started_at: string;
};
type FetchRow = {
  kind: string; dependency_name: string; action_name: string; predictor_source: string;
  speculative: boolean; consumed: boolean; wasted: boolean; confidence: number;
  fetch_duration_ms: number; issued_at_turn: number; consumed_at_turn: number | null;
  payload_summary: string; error: boolean;
};
type JourneyTurn = {
  turn_index: number; user_message: string; assistant_message: string; state: string; action: string;
  cohort: string; mood: string; instruction_hit: boolean; duration_ms: number; created_at: string;
  debug: {
    prompt_text?: string; context_block?: string; reply_source?: string; respond_ms?: number;
    retrieval?: Record<string, string>;
  } | null;
};
type Journey = {
  session_id: string; sop_id: string; sop_version: number; status: string;
  terminal_outcome: string | null;
  definition: Record<string, unknown> | null;
  prompt_bindings: Record<string, { version: number; content: string }>;
  turns: JourneyTurn[];
};

const OUTCOME_TONE: Record<string, string> = { success: "good", failure: "crit", abandoned: "warn" };

export default function SessionsView() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [journey, setJourney] = useState<Journey | null>(null);
  const [journeyNote, setJourneyNote] = useState("");
  const [node, setNode] = useState<{ name: string; kind: "action" | "state" } | null>(null);
  const [audit, setAudit] = useState<FetchRow[] | null>(null);
  const [auditNote, setAuditNote] = useState("");

  const refresh = useCallback(async () => setSessions(await api<Session[]>("GET", "/sessions")), []);
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const open = async (id: string) => {
    setSelected(id);
    setNode(null);
    setJourney(null);
    setJourneyNote("");
    setAudit(null);
    setAuditNote("");
    try {
      setJourney(await api<Journey>("GET", `/sessions/${id}/journey`));
    } catch {
      setJourneyNote("journey endpoint activates on the next backend restart");
    }
    try {
      const a = await api<{ fetches: FetchRow[] }>("GET", `/sessions/${id}/fetches`);
      setAudit(a.fetches);
    } catch {
      setAuditNote("audit endpoint activates on the next backend restart");
    }
  };

  // visit counts per node name (actions AND states) for graph highlighting
  const visits: Record<string, number> = {};
  for (const t of journey?.turns ?? []) {
    if (t.action) visits[t.action] = (visits[t.action] ?? 0) + 1;
    if (t.state) visits[t.state] = (visits[t.state] ?? 0) + 1;
  }

  const def = journey?.definition as
    | { agent_actions?: Array<{ name: string; must_say?: string[]; prompt_blocks?: string[] }> }
    | null
    | undefined;
  const nodeTurns = node
    ? (journey?.turns ?? []).filter((t) => (node.kind === "action" ? t.action === node.name : t.state === node.name))
    : [];
  const nodeAction = node?.kind === "action" ? def?.agent_actions?.find((a) => a.name === node.name) : undefined;

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Sessions</h1>
          <p>Recent conversations mapped onto their SOP graph — click a node to see the turns and the prompt that ran there.</p>
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
                      <tr key={s.session_id} className={selected === s.session_id ? "sel" : ""} onClick={() => open(s.session_id)} style={{ cursor: "pointer" }}>
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
            <h3>Turn inspector</h3>
            {node && <span className="sub">{node.kind}: {node.name} · {nodeTurns.length} turn{nodeTurns.length === 1 ? "" : "s"}</span>}
          </div>
          <div className="cbody">
            {!selected ? (
              <div className="empty">Select a session, then click a node on its journey graph below.</div>
            ) : !node ? (
              <div className="empty">Click a highlighted node on the journey graph to inspect what happened there.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10, maxHeight: 400, overflow: "auto" }}>
                {nodeAction && (nodeAction.must_say?.length || nodeAction.prompt_blocks?.length) ? (
                  <div style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px" }}>
                    <div style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>PROMPT AT THIS STAGE</div>
                    {(nodeAction.prompt_blocks ?? []).map((b) => (
                      <div key={b} style={{ marginBottom: 8 }}>
                        <span className="chip warn"><span className="cd" />{b} v{journey?.prompt_bindings[b]?.version ?? "?"}</span>
                        <div style={{ fontSize: 12.5, marginTop: 4, whiteSpace: "pre-wrap" }}>
                          {journey?.prompt_bindings[b]?.content ?? <i style={{ color: "var(--muted)" }}>(not pinned in this session)</i>}
                        </div>
                      </div>
                    ))}
                    {(nodeAction.must_say ?? []).map((m, i) => (
                      <div key={i} style={{ fontSize: 12.5, color: "var(--text2)" }}>must say: “{m}”</div>
                    ))}
                  </div>
                ) : null}
                {nodeTurns.length === 0 && <div className="empty">The conversation never visited this node.</div>}
                {nodeTurns.map((t) => (
                  <div key={t.turn_index} style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px" }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 6 }}>
                      <span className="chip"><span className="cd" />turn {t.turn_index}</span>
                      {t.state && <span className="chip accent"><span className="cd" />{t.state}</span>}
                      {t.mood && <span className="chip"><span className="cd" />{t.mood}</span>}
                      {t.instruction_hit && <span className="chip good"><span className="cd" />pre-drafted reply served</span>}
                      <span className="chip"><span className="cd" />{t.duration_ms} ms</span>
                    </div>
                    <div style={{ fontSize: 13, marginBottom: 4 }}><b>User:</b> {t.user_message || <i style={{ color: "var(--muted)" }}>(none)</i>}</div>
                    <div style={{ fontSize: 13 }}><b>Agent:</b> {t.assistant_message || <i style={{ color: "var(--muted)" }}>(none)</i>}</div>
                    {t.debug?.prompt_text && (
                      <details style={{ marginTop: 6 }}>
                        <summary style={{ fontSize: 11.5, color: "var(--muted)", cursor: "pointer" }}>
                          what actually ran — prompt{t.debug.reply_source ? ` · reply: ${t.debug.reply_source}` : ""}
                          {t.debug.respond_ms !== undefined ? ` · respond ${t.debug.respond_ms} ms` : ""}
                          {t.debug.retrieval && Object.keys(t.debug.retrieval).length ? ` · ${Object.keys(t.debug.retrieval).length} retrievals` : ""}
                        </summary>
                        <pre style={{ whiteSpace: "pre-wrap", fontSize: 11.5, lineHeight: 1.5, maxHeight: 180, overflow: "auto", background: "var(--surface2)", borderRadius: 8, padding: "8px 10px", margin: "6px 0 0", color: "var(--text2)" }}>{t.debug.prompt_text}</pre>
                      </details>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {selected && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="chead">
            <h3>Conversation journey</h3>
            {journey && (
              <span className="sub num">
                {journey.turns.length} turns · SOP v{journey.sop_version}
                {journey.terminal_outcome ? ` · ${journey.terminal_outcome}` : ""}
              </span>
            )}
          </div>
          <div className="cbody">
            {journeyNote ? (
              <div className="empty">{journeyNote}</div>
            ) : !journey ? (
              <div className="spin" />
            ) : !journey.definition ? (
              <div className="empty">The SOP version this session ran is no longer available.</div>
            ) : (
              <GraphView
                def={journey.definition}
                sopId={`journey-${journey.sop_id}`}
                visits={visits}
                onSelect={(name, kind) => setNode({ name, kind })}
              />
            )}
          </div>
        </div>
      )}

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
