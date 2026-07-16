// Autopilot A/B (Feature B): run the customer simulator against two SOP
// versions through the real runtime; chart accuracy / response time /
// satisfaction per turn. Pure-SVG charts, design tokens only.
import { FlaskConical, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import Transcript from "./Transcript";

type SopMeta = { id: string; name: string; latest_version: number };
type VersionMeta = { version: number; status: string };
type PerTurn = { turn_index: number; n: number; accuracy: number | null; response_ms: number | null; satisfaction: number | null };
type SessionDetail = { session_id: string; persona: string; outcome: string; turns: unknown[] };
type ArmResult = {
  sop_version: number;
  session_details?: SessionDetail[];
  per_turn: PerTurn[];
  sessions: number;
  outcomes: Record<string, number>;
  success_rate: number | null;
  avg_turns: number | null;
  overall: { accuracy: number | null; response_ms: number | null; satisfaction: number | null };
};
type ABTestRow = {
  id: string; sop_id: string; name: string; arm_a_version: number; arm_b_version: number;
  n_sessions: number; max_turns: number; status: string;
  progress: { completed: number; total: number } | null;
  results: { arm_a: ArmResult; arm_b: ArmResult } | null;
  error: string; created_at: string; finished_at: string | null;
};

const ARM_COLORS = { a: "var(--accent)", b: "var(--good)" };

function LineChart({
  title, unit, series, yMax, yFmt,
}: {
  title: string; unit: string;
  series: { a: Array<[number, number]>; b: Array<[number, number]> };
  yMax?: number; yFmt: (v: number) => string;
}) {
  const W = 380, H = 190, L = 46, R = 40, T = 26, B = 30;
  const pts = [...series.a, ...series.b];
  if (pts.length === 0) return null;
  const xs = pts.map((p) => p[0]);
  const maxX = Math.max(...xs, 1);
  const maxY = yMax ?? (Math.max(...pts.map((p) => p[1])) * 1.15 || 1);
  const px = (x: number) => L + (x / maxX) * (W - L - R);
  const py = (y: number) => T + (1 - y / maxY) * (H - T - B);
  const path = (s: Array<[number, number]>) => s.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(" ");
  const gridYs = [0, 0.5, 1].map((f) => f * maxY);
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px", background: "var(--surface)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={{ fontSize: 12, fontWeight: 700 }}>{title}</span>
        <span style={{ fontSize: 10.5, color: "var(--muted)" }}>{unit}</span>
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title} per turn, arm A vs arm B`}>
        {gridYs.map((y, i) => (
          <g key={i}>
            <line x1={L} y1={py(y)} x2={W - R} y2={py(y)} stroke="var(--line)" strokeWidth={1} />
            <text x={L - 6} y={py(y) + 3.5} textAnchor="end" style={{ fill: "var(--muted)", fontSize: 9.5 }}>{yFmt(y)}</text>
          </g>
        ))}
        {Array.from({ length: maxX + 1 }, (_, x) => (
          <text key={x} x={px(x)} y={H - 12} textAnchor="middle" style={{ fill: "var(--muted)", fontSize: 9.5 }}>{x}</text>
        ))}
        <text x={(L + W - R) / 2} y={H - 1} textAnchor="middle" style={{ fill: "var(--muted)", fontSize: 9 }}>turn</text>
        {(["a", "b"] as const).map((arm) => (
          <g key={arm}>
            <path d={path(series[arm])} fill="none" stroke={ARM_COLORS[arm]} strokeWidth={2} />
            {series[arm].map((p, i) => (
              <circle key={i} cx={px(p[0])} cy={py(p[1])} r={4} fill={ARM_COLORS[arm]} stroke="var(--surface)" strokeWidth={2}>
                <title>{`${arm.toUpperCase()} · turn ${p[0]}: ${yFmt(p[1])}`}</title>
              </circle>
            ))}
            {series[arm].length > 0 && (
              <text
                x={Math.min(px(series[arm][series[arm].length - 1][0]) + 7, W - 4)}
                y={py(series[arm][series[arm].length - 1][1]) + 3.5}
                style={{ fill: ARM_COLORS[arm], fontSize: 10, fontWeight: 700 }}
              >
                {arm.toUpperCase()}
              </text>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}

function ArmTile({ label, color, arm }: { label: string; color: string; arm: ArmResult }) {
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 14px", flex: 1, minWidth: 220 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
        <span style={{ fontWeight: 700, fontSize: 13 }}>{label} — SOP v{arm.sop_version}</span>
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <span className="chip good"><span className="cd" />success {arm.success_rate === null ? "—" : `${Math.round(arm.success_rate * 100)}%`}</span>
        <span className="chip"><span className="cd" />{arm.sessions} sessions · {arm.avg_turns ?? "—"} turns avg</span>
        <span className="chip accent"><span className="cd" />adherence {arm.overall.accuracy ?? "—"}</span>
        <span className="chip comm"><span className="cd" />satisfaction {arm.overall.satisfaction ?? "—"}/5</span>
        <span className="chip"><span className="cd" />{arm.overall.response_ms ? `${Math.round(arm.overall.response_ms)} ms/turn` : "—"}</span>
      </div>
    </div>
  );
}

export default function AutopilotPanel({ sops }: { sops: SopMeta[] }) {
  const [sopId, setSopId] = useState("");
  const [versions, setVersions] = useState<VersionMeta[]>([]);
  const [armA, setArmA] = useState(0);
  const [armB, setArmB] = useState(0);
  const [nSessions, setNSessions] = useState(4);
  const [test, setTest] = useState<ABTestRow | null>(null);
  const [history, setHistory] = useState<ABTestRow[]>([]);
  const [err, setErr] = useState("");
  const timer = useRef<number | null>(null);
  const [openSession, setOpenSession] = useState<string>("");

  useEffect(() => {
    if (!sopId) return;
    api<VersionMeta[]>("GET", `/sops/${sopId}/versions`).then(setVersions).catch(() => setVersions([]));
    api<ABTestRow[]>("GET", `/abtests?sop_id=${sopId}`).then((h) => {
      setHistory(h);
      const active = h.find((t) => t.status === "running");
      setTest(active ?? h[0] ?? null);
    }).catch(() => setHistory([]));
    setArmA(0);
    setArmB(0);
  }, [sopId]);

  const poll = useCallback((id: string) => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = window.setInterval(async () => {
      try {
        const t = await api<ABTestRow>("GET", `/abtests/${id}`);
        setTest(t);
        if (t.status !== "running" && timer.current) {
          window.clearInterval(timer.current);
          timer.current = null;
          setHistory(await api<ABTestRow[]>("GET", `/abtests?sop_id=${t.sop_id}`));
        }
      } catch { /* transient */ }
    }, 3000);
  }, []);
  useEffect(() => {
    if (test?.status === "running" && !timer.current) poll(test.id);
    return () => { if (timer.current) { window.clearInterval(timer.current); timer.current = null; } };
  }, [test?.id, test?.status, poll]);

  const run = async () => {
    setErr("");
    try {
      const t = await api<ABTestRow>("POST", "/abtests", {
        sop_id: sopId, arm_a_version: armA, arm_b_version: armB, n_sessions: nSessions,
      });
      setTest(t);
      poll(t.id);
    } catch (e) {
      setErr(String(e));
    }
  };

  const r = test?.results;
  const seriesOf = (metric: "accuracy" | "response_ms" | "satisfaction") => ({
    a: (r?.arm_a.per_turn ?? []).filter((t) => t[metric] !== null).map((t) => [t.turn_index, t[metric] as number] as [number, number]),
    b: (r?.arm_b.per_turn ?? []).filter((t) => t[metric] !== null).map((t) => [t.turn_index, t[metric] as number] as [number, number]),
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select className="qinput" style={{ flex: "none", minWidth: 220 }} value={sopId} onChange={(e) => setSopId(e.target.value)}>
          <option value="">choose SOP…</option>
          {sops.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select className="qinput" style={{ flex: "none" }} value={armA} onChange={(e) => setArmA(Number(e.target.value))} title="Arm A (control)">
          <option value={0}>A: published</option>
          {versions.map((v) => <option key={v.version} value={v.version}>A: v{v.version} ({v.status})</option>)}
        </select>
        <select className="qinput" style={{ flex: "none" }} value={armB} onChange={(e) => setArmB(Number(e.target.value))} title="Arm B (candidate)">
          <option value={0}>B: latest</option>
          {versions.map((v) => <option key={v.version} value={v.version}>B: v{v.version} ({v.status})</option>)}
        </select>
        <select className="qinput" style={{ flex: "none" }} value={nSessions} onChange={(e) => setNSessions(Number(e.target.value))} title="Simulated sessions per arm">
          {[2, 4, 6, 8, 10].map((n) => <option key={n} value={n}>{n} sessions/arm</option>)}
        </select>
        <button className="btn primary" disabled={!sopId || test?.status === "running"} onClick={run}>
          <Play /> Run A/B
        </button>
        {history.length > 1 && (
          <select className="qinput" style={{ flex: "none" }} value={test?.id ?? ""} onChange={(e) => setTest(history.find((h) => h.id === e.target.value) ?? null)}>
            {history.map((h) => <option key={h.id} value={h.id}>{h.created_at.slice(5, 16).replace("T", " ")} · v{h.arm_a_version} vs v{h.arm_b_version} ({h.status})</option>)}
          </select>
        )}
      </div>
      {err && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{err}</span>}
      {!sopId && <div className="empty"><FlaskConical size={16} style={{ verticalAlign: -3 }} /> Pick an SOP, choose two versions (e.g. published vs your new draft), and the customer simulator runs both through the real runtime — same personas, alternating arms.</div>}

      {test?.status === "running" && (
        <div style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "12px 14px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 12.5 }}>
            <span><b>{test.name}</b> — simulating…</span>
            <span className="num" style={{ color: "var(--muted)" }}>{test.progress?.completed ?? 0} / {test.progress?.total ?? "?"} sessions</span>
          </div>
          <div style={{ height: 8, background: "var(--surface2)", borderRadius: 999 }}>
            <div style={{ height: 8, borderRadius: 999, background: "var(--accent)", width: `${((test.progress?.completed ?? 0) / (test.progress?.total || 1)) * 100}%`, transition: "width .6s" }} />
          </div>
        </div>
      )}
      {test?.status === "failed" && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />run failed: {test.error}</span>}

      {r && (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <ArmTile label="Arm A" color={ARM_COLORS.a} arm={r.arm_a} />
            <ArmTile label="Arm B" color={ARM_COLORS.b} arm={r.arm_b} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 10 }}>
            <LineChart title="SOP adherence" unit="judge score, 1.0 = perfect" series={seriesOf("accuracy")} yMax={1} yFmt={(v) => v.toFixed(1)} />
            <LineChart title="Response time" unit="ms per turn" series={seriesOf("response_ms")} yFmt={(v) => `${Math.round(v / 100) / 10}s`} />
            <LineChart title="User satisfaction" unit="judge score, 1–5" series={seriesOf("satisfaction")} yMax={5} yFmt={(v) => v.toFixed(1)} />
          </div>
          <p style={{ margin: 0, fontSize: 11.5, color: "var(--muted)" }}>
            Every point averages {test?.n_sessions} simulated sessions per arm at that turn (fewer if conversations ended earlier).
            Adherence and satisfaction are scored per turn by an LLM judge; response time is the server's full turn time.
            Same {test?.n_sessions} personas hit both arms in alternation, so drift affects them equally.
          </p>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 10 }}>
            {(["arm_a", "arm_b"] as const).map((armKey) => (
              <div key={armKey} style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px" }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                  <span style={{ width: 10, height: 10, borderRadius: 3, background: armKey === "arm_a" ? ARM_COLORS.a : ARM_COLORS.b }} />
                  <span style={{ fontWeight: 700, fontSize: 12.5 }}>
                    {armKey === "arm_a" ? "Arm A" : "Arm B"} sessions — click to open the full transcript
                  </span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {(r[armKey].session_details ?? []).map((sd) => (
                    <button
                      key={sd.session_id}
                      className="btn ghost sm"
                      style={{ justifyContent: "flex-start", textAlign: "left", fontSize: 12, border: openSession === sd.session_id ? "1px solid var(--accent)" : "1px solid var(--line)" }}
                      onClick={() => setOpenSession(openSession === sd.session_id ? "" : sd.session_id)}
                    >
                      <span className={"st " + (sd.outcome === "success" ? "good" : sd.outcome === "failure" ? "crit" : "warn")}>{sd.outcome}</span>
                      <span style={{ color: "var(--muted)" }}>{sd.turns.length} turns ·</span> {sd.persona}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {openSession && (
            <div className="card">
              <div className="chead">
                <h3>Transcript — what actually ran</h3>
                <span className="sub mono">{openSession.slice(0, 12)}…</span>
              </div>
              <div className="cbody" style={{ maxHeight: 520, overflow: "auto" }}>
                <Transcript sessionId={openSession} />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
