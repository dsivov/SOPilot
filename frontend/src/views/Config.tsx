// Config viewer (visualisation spike). Loads a PolarTie config.json and renders
// it as a dependency graph + status + structural validation + logical prompt
// validation. Read-only — the cheapest slice that proves the config-management
// direction, built on the existing Studio. No new backend.
import { useMemo, useState } from "react";
import ConfigGraph from "./ConfigGraph";
import { SAMPLE_CONFIG } from "../config/sampleConfig";
import { configToGraph, validateConfig, logicalPromptFindings, enabledTools, type Finding } from "../config/configModel";

const ICON: Record<Finding["level"], string> = { error: "✖", warn: "⚠", ok: "✔", info: "·" };
const COLOR: Record<Finding["level"], string> = { error: "var(--crit)", warn: "var(--warn)", ok: "var(--good)", info: "var(--muted)" };

function Findings({ items }: { items: Finding[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {items.map((f, i) => (
        <div key={i} className="lintline" style={{ display: "flex", gap: 9, alignItems: "flex-start" }}>
          <span style={{ color: COLOR[f.level], fontWeight: 700, flex: "0 0 auto" }}>{ICON[f.level]}</span>
          <span>{f.msg}</span>
        </div>
      ))}
    </div>
  );
}

export default function ConfigView() {
  const [text, setText] = useState(JSON.stringify(SAMPLE_CONFIG, null, 2));
  const [cfg, setCfg] = useState<Record<string, any>>(SAMPLE_CONFIG);
  const [err, setErr] = useState("");

  const load = () => {
    try { setCfg(JSON.parse(text)); setErr(""); } catch (e: any) { setErr(String(e?.message ?? e)); }
  };

  const graph = useMemo(() => configToGraph(cfg), [cfg]);
  const struct = useMemo(() => validateConfig(cfg), [cfg]);
  const logical = useMemo(() => logicalPromptFindings(cfg), [cfg]);
  const tools = enabledTools(cfg);
  const errCount = [...struct, ...logical].filter((f) => f.level === "error").length;

  const stat = (label: string, value: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderBottom: "1px solid var(--line)", fontSize: 13 }}>
      <b style={{ color: "var(--text)" }}>{label}</b><span className="mono" style={{ color: "var(--good)" }}>{value}</span>
    </div>
  );

  return (
    <div className="view">
      <div className="eyebrow">Config viewer · spike</div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead">
          <span>Robot config.json</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            {errCount > 0 && <span className="chip crit"><span className="cd" />{errCount} problem{errCount === 1 ? "" : "s"}</span>}
            <button className="btn sm primary" onClick={load}>Load &amp; render</button>
          </span>
        </div>
        <div className="cbody">
          <textarea className="area mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          {err && <div className="lintline" style={{ color: "var(--crit)", marginTop: 6 }}>JSON error: {err}</div>}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Dependency graph</span>
          <span className="sub" style={{ marginLeft: "auto" }}>{cfg.display_name}</span></div>
        <div className="cbody"><ConfigGraph graph={graph} /></div>
      </div>

      <div className="grid2" style={{ marginBottom: 14 }}>
        <div className="card">
          <div className="chead"><span>Status</span></div>
          <div className="cbody">
            {stat("Model · voice · lang", `${cfg.custom_config?.gpt_model ?? "gpt-realtime"} · ${cfg.voice ?? "alloy"} · ${cfg.default_language_iso || "—"}`)}
            {stat("Tools enabled", `${tools.length}`)}
            {stat("MCP servers", `${(cfg.mcp_servers ?? []).length}`)}
            {stat("Knowledge bases", `${(cfg.knowledge_base ?? []).length}`)}
            {stat("Transfer topics", `${(cfg.transfer_topics ?? []).length}`)}
            {stat("Visual hints", `${(cfg.visual_hints ?? []).length}`)}
          </div>
        </div>
        <div className="card">
          <div className="chead"><span>Validation — structural</span></div>
          <div className="cbody"><Findings items={struct} /></div>
        </div>
      </div>

      <div className="card">
        <div className="chead"><span>Logical prompt validation</span>
          <span className="sub" style={{ marginLeft: "auto" }}>freeform prompt vs config · heuristic preview (real = LLM)</span></div>
        <div className="cbody"><Findings items={logical} /></div>
      </div>
    </div>
  );
}
