// Constraint-graph Studio — visualize the form's dependency graph, edit rules by AI
// with contradiction-checking, and apply. Backed by GET/POST /formflow/graph[/edit].
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";

type GNode = {
  name: string; id: number; label: string; stage: string; stage_title: string;
  conditional: boolean; repeat_group?: string | null; repeater?: boolean;
};
type GEdge = { src: string; dst: string; expr: string; cross_stage: boolean };
type Graph = {
  form?: string; nodes: GNode[]; edges: GEdge[]; stages: { id: string; title: string }[];
  stats: {
    fields: number; conditional_fields: number; edges: number; cross_stage_edges: number;
    stages: number; dangling_refs: number; top_controllers: { name: string; controls: number }[];
  };
  health: { dangling: { field: string; missing_ref: string; expr: string }[] };
};
type Proposal = {
  field?: string; old_condition?: string; new_condition?: string; rationale?: string;
  diff?: { from: string; to: string }; applied?: boolean; error?: string;
  validation?: { valid: boolean; errors: string[]; warnings: string[] };
};

const ROW = 34;

export default function FormGraph() {
  const [g, setG] = useState<Graph | null>(null);
  const [err, setErr] = useState("");
  const [stage, setStage] = useState("");
  const [sel, setSel] = useState("");
  const [instr, setInstr] = useState("");
  const [prop, setProp] = useState<Proposal | null>(null);
  const [busy, setBusy] = useState(false);
  // live execution trace
  const [traceMode, setTraceMode] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [trace, setTrace] = useState<Record<string, { visible: boolean; answered: boolean; stale: boolean }>>({});
  const [next, setNext] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, number> | null>(null);

  const load = async () => {
    try {
      const d = await api<Graph & { error?: string }>("GET", "/formflow/graph");
      if ((d as any).error) { setErr((d as any).error); setG(null); return; }
      setErr(""); setG(d);
      setStage((s) => s || d.stages[0]?.id || "");
    } catch (e: any) { setErr(String(e?.detail ?? e?.message ?? e)); setG(null); }
  };
  useEffect(() => {
    load();
    const h = () => load();
    window.addEventListener("sopilot-project-imported", h);
    return () => window.removeEventListener("sopilot-project-imported", h);
  }, []);

  const nodes = useMemo(() => (g ? g.nodes.filter((n) => n.stage === stage) : []), [g, stage]);
  const yOf = useMemo(() => {
    const m: Record<string, number> = {};
    nodes.forEach((n, i) => { m[n.name] = 18 + i * ROW; });
    return m;
  }, [nodes]);
  const edges = useMemo(() => {
    const names = new Set(nodes.map((n) => n.name));
    return g ? g.edges.filter((e) => names.has(e.src) && names.has(e.dst)) : [];
  }, [g, nodes]);

  const selNode = g?.nodes.find((n) => n.name === sel);

  const propose = async (apply: boolean) => {
    if (!instr.trim()) return;
    setBusy(true);
    try {
      const r = await api<Proposal>("POST", "/formflow/graph/edit",
        { instruction: instr, field: sel || undefined, apply });
      setProp(r);
      if (apply && r.applied) { await load(); }
    } catch (e: any) { setProp({ error: String(e?.detail ?? e?.message ?? e) }); }
    finally { setBusy(false); }
  };

  const runTrace = async (ans: Record<string, string>) => {
    try {
      const r = await api<any>("POST", "/formflow/graph/trace", { answers: ans });
      const m: Record<string, any> = {};
      (r.fields || []).forEach((f: any) => { m[f.name] = f; });
      setTrace(m); setNext(r.next_field ?? null); setCounts(r.counts ?? null);
    } catch { /* ignore */ }
  };
  const setAnswer = (name: string, val: string) => {
    const a = { ...answers };
    if (val === "") delete a[name]; else a[name] = val;
    setAnswers(a); runTrace(a);
  };
  const toggleTrace = () => {
    if (traceMode) { setTraceMode(false); }
    else { setTraceMode(true); runTrace(answers); }
  };

  if (err) return (
    <div className="view">
      <h2>Constraint graph</h2>
      <div className="card" style={{ padding: 16, borderLeft: "3px solid var(--crit)" }}>
        {err}. Switch to the <b>project that has a published form</b> (e.g. <span className="mono">smartform</span>),
        or run the ingest pipeline first.
      </div>
    </div>
  );
  if (!g) return <div className="view"><h2>Constraint graph</h2><p className="sub">Loading…</p></div>;

  const S = g.stats;
  const svgH = Math.max(120, nodes.length * ROW + 30);

  return (
    <div className="view" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <div style={{ flex: 1 }}>
          <h2 style={{ marginBottom: 2 }}>Constraint graph — {g.form}</h2>
          <p className="sub" style={{ margin: 0, fontSize: 13 }}>
            The form's show-if rules as a live dependency graph. Edit a rule in natural language (contradiction-checked
            before apply), or turn on <b>Trace</b> to set answers and watch gating + reconcile light up in real time.
          </p>
        </div>
        <button className={"btn sm" + (traceMode ? "" : " ghost")} onClick={toggleTrace}>
          {traceMode ? "Trace: ON" : "Trace"}
        </button>
      </div>

      {traceMode && (
        <div className="card" style={{ padding: "8px 12px", display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
          {[["answered", "var(--good)"], ["next / visible-unanswered", "var(--accent)"], ["hidden (skipped)", "var(--line)"], ["stale → reconcile voids", "var(--crit)"]].map(([lab, col]) => (
            <span key={lab} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 12, height: 12, borderRadius: 3, background: col, display: "inline-block" }} />{lab}
            </span>
          ))}
          {counts && <span className="sub" style={{ marginLeft: "auto" }}>
            {counts.answered} answered · {counts.visible_unanswered} to ask · {counts.hidden} hidden · <b style={{ color: counts.stale ? "var(--crit)" : "var(--good)" }}>{counts.stale} stale</b>
            {next ? <> · next: <b className="mono">{next}</b></> : (g && Object.keys(answers).length ? " · ✓ complete" : "")}
          </span>}
          <button className="btn ghost sm" onClick={() => { setAnswers({}); setTrace({}); setNext(null); setCounts(null); runTrace({}); }}>Clear answers</button>
        </div>
      )}

      {/* stats */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {[
          ["fields", S.fields], ["conditional", S.conditional_fields], ["edges", S.edges],
          ["stages", S.stages], ["cross-stage", S.cross_stage_edges], ["dangling refs", S.dangling_refs],
        ].map(([k, v]) => (
          <div key={k as string} className="card" style={{ padding: "8px 14px", minWidth: 92 }}>
            <div style={{ fontSize: 22, fontWeight: 800, color: (k === "dangling refs" || k === "cross-stage") ? (Number(v) ? "var(--crit)" : "var(--good)") : "var(--accent)" }}>{v}</div>
            <div className="sub" style={{ fontSize: 11 }}>{k}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "180px 1fr 340px", gap: 14, alignItems: "start" }}>
        {/* stage list */}
        <div className="card" style={{ padding: 8, maxHeight: 560, overflowY: "auto" }}>
          <div className="sub" style={{ fontSize: 11, padding: "4px 6px", textTransform: "uppercase", letterSpacing: ".04em" }}>Stages</div>
          {g.stages.map((s) => (
            <button key={s.id}
              style={{
                width: "100%", textAlign: "left", fontSize: 12.5, padding: "6px 8px", border: 0,
                borderRadius: "var(--r-sm)", cursor: "pointer",
                background: s.id === stage ? "var(--accent-dim)" : "transparent",
                color: s.id === stage ? "var(--text)" : "var(--text2)",
                fontWeight: s.id === stage ? 600 : 500,
              }}
              onClick={() => { setStage(s.id); setSel(""); }}>
              {s.title}
            </button>
          ))}
        </div>

        {/* per-stage graph */}
        <div className="card" style={{ padding: 8, overflowX: "auto" }}>
          <svg viewBox={`0 0 520 ${svgH}`} style={{ width: "100%", minWidth: 480, height: svgH }}>
            <defs>
              <marker id="fg-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0 0L10 5L0 10z" fill="var(--muted)" />
              </marker>
            </defs>
            {/* edges: left-side arcs src -> dst */}
            {edges.map((e, i) => {
              const y1 = yOf[e.src], y2 = yOf[e.dst];
              if (y1 == null || y2 == null) return null;
              const bulge = 28 + Math.min(60, Math.abs(y2 - y1) / 3);
              const hot = sel && (e.src === sel || e.dst === sel);
              return (
                <path key={i} d={`M150,${y1} C${150 - bulge},${y1} ${150 - bulge},${y2} 150,${y2}`}
                  fill="none" stroke={hot ? "var(--accent)" : "var(--line)"} strokeWidth={hot ? 2 : 1.2}
                  markerEnd="url(#fg-a)" />
              );
            })}
            {/* nodes */}
            {nodes.map((n) => {
              const y = yOf[n.name];
              const active = n.name === sel;
              const t = traceMode ? trace[n.name] : undefined;
              const isNext = traceMode && next === n.name;
              let fill: string, stroke: string, dim = false;
              if (t) {
                if (t.stale) { fill = "rgba(224,87,79,.20)"; stroke = "var(--crit)"; }
                else if (t.answered) { fill = "rgba(25,184,154,.18)"; stroke = "var(--good)"; }
                else if (t.visible) { fill = "var(--accent-dim)"; stroke = "var(--accent)"; }
                else { fill = "transparent"; stroke = "var(--line)"; dim = true; }
              } else {
                fill = active ? "var(--accent-dim)" : n.conditional ? "rgba(91,141,239,.10)" : "var(--surface)";
                stroke = active ? "var(--accent)" : n.conditional ? "var(--accent)" : "var(--line)";
              }
              return (
                <g key={n.name} style={{ cursor: "pointer", opacity: dim ? 0.5 : 1 }} onClick={() => setSel(n.name)}>
                  <rect x={150} y={y - 12} width={360} height={24} rx={6} fill={fill}
                    stroke={isNext ? "var(--accent)" : stroke} strokeWidth={isNext || active ? 2.5 : 1.2}
                    strokeDasharray={isNext ? "5 3" : undefined} />
                  <text x={158} y={y + 4} style={{ font: "700 11px var(--mono)", fill: "var(--text)" }}>{n.name}{n.repeater ? " 🔁" : ""}</text>
                  <text x={200} y={y + 4} style={{ font: "10px system-ui,sans-serif", fill: "var(--muted)" }}>{(n.label || "").slice(0, 44)}</text>
                </g>
              );
            })}
          </svg>
        </div>

        {/* right: field detail + AI edit + health */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="card" style={{ padding: 12 }}>
            <div className="sub" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>AI rule editor</div>
            {selNode ? (
              <div style={{ margin: "6px 0" }}>
                <b className="mono">{selNode.name}</b> — {selNode.label}
                <div style={{ marginTop: 4, fontSize: 12 }}>show-if: <span className="mono">{g.nodes.find(n => n.name === sel)?.conditional ? (g.edges.find(e => e.dst === sel)?.expr || "(has condition)") : "(always shown)"}</span></div>
              </div>
            ) : <div className="sub" style={{ fontSize: 12, margin: "6px 0" }}>Pick a field in the graph, or just describe the change.</div>}

            {traceMode && selNode && (
              <div style={{ margin: "0 0 10px", padding: 8, background: "var(--bg)", borderRadius: "var(--r-sm)" }}>
                <div className="sub" style={{ fontSize: 11, marginBottom: 4 }}>Set answer (live trace)</div>
                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  {["Yes", "No"].map((v) => (
                    <button key={v} className={"btn ghost sm" + (answers[sel] === v ? " " : "")}
                      style={answers[sel] === v ? { borderColor: "var(--accent)", color: "var(--accent)" } : undefined}
                      onClick={() => setAnswer(sel, v)}>{v}</button>
                  ))}
                  <input value={answers[sel] ?? ""} onChange={(e) => setAnswer(sel, e.target.value)}
                    placeholder="or a value / number"
                    style={{ flex: 1, minWidth: 90, fontSize: 12, background: "var(--surface)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: "var(--r-sm)", padding: "5px 8px", outline: "none" }} />
                  <button className="btn ghost sm" onClick={() => setAnswer(sel, "")}>clear</button>
                </div>
                {trace[sel] && <div className="sub" style={{ fontSize: 11, marginTop: 5 }}>
                  state: {trace[sel].stale ? <b style={{ color: "var(--crit)" }}>stale (reconcile voids)</b>
                    : trace[sel].answered ? <b style={{ color: "var(--good)" }}>answered</b>
                      : trace[sel].visible ? <b style={{ color: "var(--accent)" }}>visible — to ask</b>
                        : <b style={{ color: "var(--muted)" }}>hidden (skipped)</b>}
                </div>}
              </div>
            )}
            <textarea value={instr} onChange={(e) => setInstr(e.target.value)} rows={3}
              placeholder={selNode ? `e.g. only show ${selNode.name} if the patient was admitted` : "e.g. only ask Q43 if Q42 is yes"}
              style={{ width: "100%", fontSize: 13, resize: "vertical", background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: "var(--r-sm)", padding: "7px 9px", fontFamily: "inherit", outline: "none" }} />
            <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
              <button className="btn sm" disabled={busy || !instr.trim()} onClick={() => propose(false)}>Propose</button>
              <button className="btn ghost sm" disabled={busy || !prop?.validation?.valid} onClick={() => propose(true)} title="Write the change into the form (map block)">Apply</button>
            </div>

            {prop && (
              <div style={{ marginTop: 10, fontSize: 12.5 }}>
                {prop.error ? <div style={{ color: "var(--crit)" }}>{prop.error}</div> : (
                  <>
                    <div><b>{prop.field}</b>{prop.rationale ? <span className="sub"> — {prop.rationale}</span> : null}</div>
                    <div style={{ margin: "6px 0", padding: 8, background: "var(--surface)", borderRadius: 6 }}>
                      <div className="mono" style={{ color: "var(--muted)" }}>from: {prop.diff?.from}</div>
                      <div className="mono" style={{ color: "var(--accent)" }}>to:&nbsp;&nbsp; {prop.diff?.to}</div>
                    </div>
                    <div style={{ color: prop.validation?.valid ? "var(--good)" : "var(--crit)", fontWeight: 700 }}>
                      {prop.validation?.valid ? "✓ valid" : "✗ invalid"}{prop.applied ? " · applied ✓" : ""}
                    </div>
                    {prop.validation?.errors?.map((x, i) => <div key={i} style={{ color: "var(--crit)" }}>• {x}</div>)}
                    {prop.validation?.warnings?.map((x, i) => <div key={i} style={{ color: "var(--warn, #d98c00)" }}>⚠ {x}</div>)}
                  </>
                )}
              </div>
            )}
          </div>

          <div className="card" style={{ padding: 12 }}>
            <div className="sub" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>Hub controllers</div>
            {S.top_controllers.slice(0, 6).map((c) => (
              <div key={c.name} style={{ display: "flex", alignItems: "center", gap: 6, margin: "4px 0", fontSize: 12 }}>
                <span className="mono" style={{ minWidth: 64 }}>{c.name}</span>
                <div style={{ height: 10, width: `${Math.max(6, c.controls * 6)}px`, background: "var(--accent)", borderRadius: 3 }} />
                <span className="sub">{c.controls}</span>
              </div>
            ))}
            <div className="sub" style={{ fontSize: 11, marginTop: 8 }}>
              Health: {S.dangling_refs === 0 ? "✓ no dangling refs" : `⚠ ${S.dangling_refs} dangling`} · {S.cross_stage_edges} cross-stage
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
