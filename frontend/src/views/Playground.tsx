import { CircleStop, MessageSquarePlus, Mic, MicOff, Send } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import AutopilotPanel from "./Autopilot";
import { VoiceCall } from "./voiceCall";

type SopMeta = { id: string; name: string; latest_version: number };
type Msg = { role: "user" | "assistant"; content: string };
type Trace = {
  turn_index: number;
  chosen_action: string;
  classification: { cohort: string; state: string; mood: string };
  picks: Array<{ dependency_name: string; payload_summary: string; predictor_source: string }>;
  consume_stats: Record<string, number>;
  instruction_hit: boolean;
  rerank_ms: number;
  total_ms: number;
};

export default function PlaygroundView() {
  const [sops, setSops] = useState<SopMeta[]>([]);
  const [sopId, setSopId] = useState("");
  const [mode, setMode] = useState("");
  const [sessionMode, setSessionMode] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [poolSize, setPoolSize] = useState(0);
  const [terminal, setTerminal] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [voice, setVoice] = useState<VoiceCall | null>(null);
  const [voiceStatus, setVoiceStatus] = useState("");
  const [panel, setPanel] = useState<"live" | "autopilot">("live");
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<SopMeta[]>("GET", "/sops").then(setSops).catch(() => undefined);
  }, []);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [msgs]);

  const start = useCallback(async () => {
    setError("");
    try {
      const r = await api("POST", "/sessions", { sop_id: sopId === "__auto__" ? "" : sopId, subsystems: mode });
      setSessionId(r.session_id);
      setSessionMode(mode);
      setMsgs([]);
      setTraces([]);
      setTerminal(null);
      setPoolSize(0);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError("This SOP has no published version — publish it in the Studio first.");
      else setError(String(e));
    }
  }, [sopId]);

  const stopVoice = useCallback(() => {
    voice?.stop();
    setVoice(null);
    setVoiceStatus("");
  }, [voice]);

  const startVoice = useCallback(async () => {
    if (!sessionId || voice) return;
    setError("");
    const call = new VoiceCall(sessionId, {
      onStatus: setVoiceStatus,
      onUserUtterance: (text) => setMsgs((m) => [...m, { role: "user", content: text }]),
      onAgentReply: (text) => setMsgs((m) => [...m, { role: "assistant", content: text }]),
      onTurnResult: (r) => {
        setTraces((t) => [
          {
            turn_index: r.turn.turn_index,
            chosen_action: r.turn.chosen_action,
            classification: r.classification,
            picks: r.turn.picks,
            consume_stats: r.turn.consume_stats,
            instruction_hit: r.turn.instruction_hit,
            rerank_ms: r.turn.rerank_ms,
            total_ms: r.plan_ms,
          },
          ...t,
        ]);
        api("GET", `/sessions/${sessionId}/pool`).then((snap) => setPoolSize(snap.size)).catch(() => undefined);
        if (r.terminal) setTerminal(r.terminal);
      },
      onEnded: (reason) => {
        setVoiceStatus(`call ended (${reason})`);
        setVoice(null);
        if (reason.startsWith("terminal:")) {
          const outcome = reason.split(":")[1].trim();
          api("POST", `/sessions/${sessionId}/outcome`, { outcome }).catch(() => undefined);
          api("POST", `/sessions/${sessionId}/end`, {}).catch(() => undefined);
        }
      },
    });
    try {
      await call.start();
      setVoice(call);
    } catch (e) {
      setError(`voice: ${e}`);
      call.stop();
    }
  }, [sessionId, voice]);

  const endSession = useCallback(
    async (outcome: string | null) => {
      if (!sessionId) return;
      stopVoice();
      if (outcome) await api("POST", `/sessions/${sessionId}/outcome`, { outcome }).catch(() => undefined);
      await api("POST", `/sessions/${sessionId}/end`, {}).catch(() => undefined);
      setSessionId("");
      setTerminal(null);
    },
    [sessionId, stopVoice],
  );

  const send = async () => {
    if (!input.trim() || !sessionId || busy) return;
    const text = input.trim();
    setInput("");
    setMsgs((m) => [...m, { role: "user", content: text }]);
    setBusy(true);
    setError("");
    try {
      const r = await api("POST", `/sessions/${sessionId}/converse`, { user_message: text });
      setMsgs((m) => [...m, { role: "assistant", content: r.reply }]);
      setTraces((t) => [
        {
          turn_index: r.turn.turn_index,
          chosen_action: r.turn.chosen_action,
          classification: r.classification,
          picks: r.turn.picks,
          consume_stats: r.turn.consume_stats,
          instruction_hit: r.turn.instruction_hit,
          rerank_ms: r.turn.rerank_ms,
          total_ms: r.total_ms,
        },
        ...t,
      ]);
      const snap = await api("GET", `/sessions/${sessionId}/pool`);
      setPoolSize(snap.size);
      if (r.terminal) {
        setTerminal(r.terminal);
        await endSession(r.terminal);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const last = traces[0];

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Playground</h1>
          <p>
            {panel === "live"
              ? "Talk to a published SOP over the text channel — with the supervisor's X-ray per turn."
              : "Autopilot A/B: the customer simulator drives two SOP versions through the real runtime and charts them per turn."}
          </p>
        </div>
        <div className="actions">
          <div style={{ display: "flex", border: "1px solid var(--line)", borderRadius: 10, overflow: "hidden" }}>
            <button className={"btn ghost sm"} style={panel === "live" ? { background: "var(--accent-dim)", color: "var(--accent)", borderRadius: 0 } : { borderRadius: 0 }} onClick={() => setPanel("live")}>Live chat</button>
            <button className={"btn ghost sm"} style={panel === "autopilot" ? { background: "var(--accent-dim)", color: "var(--accent)", borderRadius: 0 } : { borderRadius: 0 }} onClick={() => setPanel("autopilot")}>Autopilot A/B</button>
          </div>
          {panel === "live" && sessionId && (
            <button className="btn" onClick={() => endSession("abandoned")}>
              <CircleStop /> End session
            </button>
          )}
        </div>
      </div>

      {panel === "autopilot" ? (
        <div className="card">
          <div className="chead"><h3>Autopilot A/B</h3></div>
          <div className="cbody">
            <AutopilotPanel sops={sops} />
          </div>
        </div>
      ) : !sessionId ? (
        <div className="card" style={{ maxWidth: 560 }}>
          <div className="chead"><h3>Start a conversation</h3></div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <select className="qinput" value={sopId} onChange={(e) => setSopId(e.target.value)} style={{ flex: "none" }}>
              <option value="">Choose an SOP…</option>
              <option value="__auto__">✨ Auto — let the router pick from the conversation (D-11)</option>
              {sops.map((s) => (
                <option key={s.id} value={s.id}>{s.name} (v{s.latest_version})</option>
              ))}
            </select>
            <select className="qinput" value={mode} onChange={(e) => setMode(e.target.value)} style={{ flex: "none" }} title="Which subsystems run for this session (D-9)">
              <option value="">subsystems: project default</option>
              <option value="both">both — SOP management + predicted retrieval</option>
              <option value="sop">sop only — prompts/tracking, live data, no speculation</option>
              <option value="retrieval">retrieval only — prediction + context block, no prompt management</option>
            </select>
            {error && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{error}</span>}
            <button className="btn primary" disabled={!sopId} onClick={start}>
              <MessageSquarePlus /> Start session
            </button>
          </div>
        </div>
      ) : (
        <div className="grid2">
          <div className="card" style={{ display: "flex", flexDirection: "column" }}>
            <div className="chead">
              <h3>Conversation</h3>
              {voiceStatus && <span className="chip accent"><span className="cd" />{voiceStatus}</span>}
              <span className="sub" style={{ display: "flex", gap: 6, alignItems: "center" }}>
                {!voice ? (
                  <button className="btn sm" title="Start a live voice call on this session" onClick={startVoice} disabled={!!terminal}>
                    <Mic /> Voice call
                  </button>
                ) : (
                  <button className="btn sm" title="Hang up" onClick={stopVoice}>
                    <MicOff /> Hang up
                  </button>
                )}
                <span className="mono" style={{ fontSize: 11 }}>{sessionId.slice(0, 10)}…</span>
              </span>
            </div>
            <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10, flex: 1 }}>
              <div className="chatlog" ref={logRef} style={{ maxHeight: 440, minHeight: 260 }}>
                {msgs.length === 0 && <div className="empty">Say hello — you're the customer.</div>}
                {msgs.map((m, i) => (
                  <div key={i} className={"msg " + (m.role === "user" ? "user" : "assistant")}>{m.content}</div>
                ))}
                {busy && <div className="spin" />}
                {terminal && (
                  <span className={"chip " + (terminal === "success" ? "good" : "crit")} style={{ alignSelf: "center" }}>
                    <span className="cd" />conversation ended: {terminal}
                  </span>
                )}
              </div>
              {error && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{error}</span>}
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  className="qinput"
                  placeholder={terminal ? "Session ended — start a new one" : "Type as the customer…"}
                  disabled={busy || !!terminal}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && send()}
                />
                <button className="btn primary" disabled={busy || !!terminal || !input.trim()} onClick={send}>
                  <Send />
                </button>
              </div>
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
            <div className="card">
              <div className="chead">
                <h3>Supervisor X-ray</h3>
                {sessionMode && <span className="chip comm"><span className="cd" />{sessionMode} mode</span>}
                <span className="sub num">pool: {poolSize} items</span>
              </div>
              <div className="cbody">
                {!last ? (
                  <div className="empty">Per-turn trace appears after the first message.</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <span className="chip comm"><span className="cd" />{last.chosen_action}</span>
                      {last.classification.state && <span className="chip accent"><span className="cd" />{last.classification.state}</span>}
                      {last.classification.cohort && <span className="chip">{last.classification.cohort}</span>}
                      {last.classification.mood && <span className="chip">{last.classification.mood}</span>}
                      {last.instruction_hit && <span className="chip good"><span className="cd" />pre-drafted instruction used</span>}
                    </div>
                    <div style={{ fontSize: 12.5, color: "var(--text2)" }} className="num">
                      turn {last.turn_index} · total {last.total_ms} ms · context selection {last.rerank_ms} ms ·
                      pool hits {last.consume_stats.consumed ?? 0} · live waits {last.consume_stats.live ?? 0} ·
                      hidden {(last.consume_stats.latency_hidden_ms ?? 0)} ms
                    </div>
                    {last.picks.length > 0 && (
                      <div>
                        <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 4 }}>
                          Injected context (speculative)
                        </div>
                        {last.picks.map((p, i) => (
                          <div key={i} className="lintline">
                            <span className="chip accent" style={{ flex: "0 0 auto" }}>{p.dependency_name}</span>
                            <span style={{ fontSize: 12 }}>{p.payload_summary.slice(0, 80)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
            <div className="card" style={{ flex: 1 }}>
              <div className="chead"><h3>Turn history</h3></div>
              <div className="cbody" style={{ padding: 0 }}>
                {traces.length === 0 ? (
                  <div className="empty">—</div>
                ) : (
                  <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 260 }}>
                    <table className="table">
                      <thead><tr><th>#</th><th>Action</th><th>State</th><th>Hits</th><th>ms</th></tr></thead>
                      <tbody>
                        {traces.map((t) => (
                          <tr key={t.turn_index}>
                            <td className="mono num">{t.turn_index}</td>
                            <td style={{ fontSize: 12.5 }}>{t.chosen_action}</td>
                            <td style={{ fontSize: 12.5, color: "var(--text2)" }}>{t.classification.state || "—"}</td>
                            <td className="mono num">{t.consume_stats.consumed ?? 0}/{(t.consume_stats.consumed ?? 0) + (t.consume_stats.live ?? 0)}</td>
                            <td className="mono num">{t.total_ms}</td>
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
      )}
    </div>
  );
}
