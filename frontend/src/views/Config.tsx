// Config viewer (visualisation spike). Renders a real PolarTie config.json as a
// dependency graph + status + structural validation + MCP-introspection-vs-prompt
// + logical prompt validation. Read-only. Defaults to the real AENA robot config.
import { useMemo, useState } from "react";
import ConfigGraph from "./ConfigGraph";
import AENA from "../config/aenaConfig.json";
import { SAMPLE_CONFIG } from "../config/sampleConfig";
import { MCP_INTROSPECTION } from "../config/mcpIntrospection";
import { configToGraph, validateConfig, promptMcpFindings, logicalPromptFindings, enabledTools, type Finding } from "../config/configModel";

const ICON: Record<Finding["level"], string> = { error: "✖", warn: "⚠", ok: "✔", info: "·" };
const COLOR: Record<Finding["level"], string> = { error: "var(--crit)", warn: "var(--warn)", ok: "var(--good)", info: "var(--muted)" };

function Findings({ items }: { items: Finding[] }) {
  if (!items.length) return <div className="empty" style={{ padding: "8px 0" }}>Nothing to report.</div>;
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
  const [text, setText] = useState(JSON.stringify(AENA, null, 2));
  const [cfg, setCfg] = useState<Record<string, any>>(AENA as Record<string, any>);
  const [err, setErr] = useState("");

  const load = (v: string) => { try { setCfg(JSON.parse(v)); setErr(""); } catch (e: any) { setErr(String(e?.message ?? e)); } };
  const preset = (c: any) => { const s = JSON.stringify(c, null, 2); setText(s); setCfg(c); setErr(""); };

  const graph = useMemo(() => configToGraph(cfg, MCP_INTROSPECTION), [cfg]);
  const struct = useMemo(() => validateConfig(cfg), [cfg]);
  const mcp = useMemo(() => promptMcpFindings(cfg, MCP_INTROSPECTION), [cfg]);
  const logical = useMemo(() => logicalPromptFindings(cfg), [cfg]);
  const tools = enabledTools(cfg);
  const mcpToolCount = (cfg.mcp_servers ?? []).reduce((n: number, s: any) => {
    const info = MCP_INTROSPECTION[s.url]; return n + (info && !info.error ? info.tools.filter((t) => !t.startsWith("polartie_")).length : 0);
  }, 0);
  const problems = [...struct, ...mcp, ...logical].filter((f) => f.level === "error").length;

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
            <button className="btn ghost sm" onClick={() => preset(AENA)}>AENA (real)</button>
            <button className="btn ghost sm" onClick={() => preset(SAMPLE_CONFIG)}>Sample</button>
            {problems > 0 && <span className="chip crit"><span className="cd" />{problems} problem{problems === 1 ? "" : "s"}</span>}
            <button className="btn sm primary" onClick={() => load(text)}>Load &amp; render</button>
          </span>
        </div>
        <div className="cbody">
          <textarea className="area mono" rows={7} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          {err && <div className="lintline" style={{ color: "var(--crit)", marginTop: 6 }}>JSON error: {err}</div>}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Dependency graph</span><span className="sub" style={{ marginLeft: "auto" }}>{cfg.display_name}</span></div>
        <div className="cbody"><ConfigGraph graph={graph} /></div>
      </div>

      <div className="grid2" style={{ marginBottom: 14 }}>
        <div className="card">
          <div className="chead"><span>Status</span></div>
          <div className="cbody">
            {stat("Model · voice · lang", `${cfg.custom_config?.gpt_model ?? "gpt-realtime"} · ${cfg.voice ?? "alloy"} · ${cfg.default_language_iso || "—"}`)}
            {stat("Tools enabled", `${tools.length}`)}
            {stat("MCP servers · tools", `${(cfg.mcp_servers ?? []).length} · ${mcpToolCount} introspected`)}
            {stat("Knowledge bases", `${(cfg.knowledge_base ?? []).length}`)}
            {stat("Transfer topics", `${(cfg.transfer_topics ?? []).length}`)}
            {stat("Prompt length", `${String(cfg.prompt ?? "").length} chars`)}
          </div>
        </div>
        <div className="card">
          <div className="chead"><span>Validation — structural</span></div>
          <div className="cbody"><Findings items={struct} /></div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>MCP tools ↔ prompt</span>
          <span className="sub" style={{ marginLeft: "auto" }}>introspected live via list_tools · does the prompt reflect the real tools?</span></div>
        <div className="cbody"><Findings items={mcp} /></div>
      </div>

      <div className="card">
        <div className="chead"><span>Logical prompt validation</span>
          <span className="sub" style={{ marginLeft: "auto" }}>freeform prompt vs config · heuristic preview (real = LLM)</span></div>
        <div className="cbody"><Findings items={logical} /></div>
      </div>
    </div>
  );
}
