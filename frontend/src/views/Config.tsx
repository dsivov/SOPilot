// Config viewer (visualisation spike). Renders a robot config.json as a
// dependency graph + status + structural validation + MCP-introspection-vs-prompt
// + logical prompt validation. Read-only. Defaults to a real (sanitized)
// production config from the first customer deployment — an example, not a binding.
// Also the USER stage of config management: enforces the admin's PUBLISHED
// ruleset (Config admin → Save & publish) against the loaded config.
import { useEffect, useMemo, useState } from "react";
import ConfigGraph from "./ConfigGraph";
import EXAMPLE from "../config/exampleConfig.json";
import { SAMPLE_CONFIG } from "../config/sampleConfig";
import { MCP_INTROSPECTION } from "../config/mcpIntrospection";
import { configToGraph, validateConfig, promptMcpFindings, logicalPromptFindings, enabledTools, availableToolNames, type Finding, type Introspection } from "../config/configModel";
import { ruleFindings, seedRules, type Rule } from "../config/rules";
import GuidedEditor from "./ConfigEdit";
import { api, getCreds } from "../api";

// The working config is persisted per project in this browser, so edits survive
// navigating away and reloads (previously it reset to the example on every mount
// — edits looked "saved" on the graph but were lost on tab switch). Export /
// "Download robot config" is the durable, cross-device artifact; this is the
// local working copy.
const cfgStoreKey = () => `sopilot-config:${getCreds().project || "default"}`;
function loadStoredConfig(): Record<string, any> | null {
  try { const s = localStorage.getItem(cfgStoreKey()); return s ? JSON.parse(s) : null; } catch { return null; }
}

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
  const initial = loadStoredConfig() ?? (EXAMPLE as Record<string, any>);
  const [text, setText] = useState(JSON.stringify(initial, null, 2));
  const [cfg, setCfg] = useState<Record<string, any>>(initial);
  const [err, setErr] = useState("");
  const [intro, setIntro] = useState<Introspection>(MCP_INTROSPECTION);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [introMsg, setIntroMsg] = useState("");
  const [logicalLive, setLogicalLive] = useState<Finding[] | null>(null);
  const [busy2, setBusy2] = useState(false);
  // Admin-published constraint rules (stage-1 → user-stage handoff). null until
  // fetched; falls back to the built-in seed when nothing is published yet.
  const [adminRules, setAdminRules] = useState<Rule[] | null>(null);
  const [adminVersion, setAdminVersion] = useState<number | null>(null);
  const [renderNotes, setRenderNotes] = useState<string[] | null>(null);
  const [renderBusy, setRenderBusy] = useState(false);

  // Write-back: resolve connector/secret references server-side into the
  // deploy-ready config.json the robot consumes, and download it.
  const downloadRobot = async () => {
    setRenderBusy(true); setRenderNotes(null);
    try {
      const r = await api<{ config: any; notes: string[] }>("POST", "/config/render-robot", { config: cfg });
      const url = URL.createObjectURL(new Blob([JSON.stringify(r.config, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: `${(cfg.display_name || "robot").toLowerCase().replace(/\s+/g, "-")}-config.json` });
      a.click(); URL.revokeObjectURL(url);
      setRenderNotes(r.notes);
    } catch (e: any) { setRenderNotes([`Render failed: ${e?.message ?? e}`]); } finally { setRenderBusy(false); }
  };

  useEffect(() => {
    api<{ published_version: number | null; published_rules: Rule[] | null }>("GET", "/config/ruleset")
      .then((r) => { if (r.published_rules) { setAdminRules(r.published_rules); setAdminVersion(r.published_version); } })
      .catch(() => { /* backend down — seed fallback below */ });
  }, []);

  // Persist the working config per project so edits survive navigation & reload.
  useEffect(() => {
    try { localStorage.setItem(cfgStoreKey(), JSON.stringify(cfg)); } catch { /* quota/serialization — non-fatal */ }
  }, [cfg]);

  const load = (v: string) => { try { setCfg(JSON.parse(v)); setErr(""); setLogicalLive(null); } catch (e: any) { setErr(String(e?.message ?? e)); } };
  const preset = (c: any) => { setText(JSON.stringify(c, null, 2)); setCfg(c); setErr(""); setIntro(MCP_INTROSPECTION); setLive(false); setLogicalLive(null); };
  const resetToExample = () => { try { localStorage.removeItem(cfgStoreKey()); } catch { /* ignore */ } preset(EXAMPLE); };

  const validate = async () => {
    setBusy2(true);
    try {
      const r = await api<{ findings: Finding[] }>("POST", "/config/validate-prompt", {
        prompt: cfg.prompt ?? "",
        available_tools: availableToolNames(cfg, intro),
        transfer_topics: (cfg.transfer_topics ?? []).map((t: any) => t.function_tag ?? t.topic_id),
        language: cfg.default_language_iso ?? "",
      });
      setLogicalLive(r.findings ?? []);
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setLogicalLive([{ level: "warn", msg: m.includes("Not Found") ? "Validation endpoint not found — restart the backend." : `Prompt validation failed: ${m}` }]);
    } finally { setBusy2(false); }
  };

  const introspect = async () => {
    const servers = (cfg.mcp_servers ?? []).map((m: any) => ({ url: m.url, authorization: m.authorization }));
    if (!servers.length) return;
    setBusy(true);
    try {
      const r = await api<{ results: Array<{ url: string; tools?: string[]; error?: string }> }>("POST", "/config/introspect-mcp", { servers });
      const map: Introspection = {};
      for (const res of r.results) map[res.url] = res.error ? { tools: [], error: res.error } : { tools: res.tools ?? [] };
      setIntro(map); setLive(true); setIntroMsg("");
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setIntroMsg(m.includes("Not Found") ? "Introspection endpoint not found — the backend needs the /config/introspect-mcp route (restart it)." : `Introspection failed: ${m}`);
    } finally { setBusy(false); }
  };

  const graph = useMemo(() => configToGraph(cfg, intro), [cfg, intro]);
  const struct = useMemo(() => validateConfig(cfg), [cfg]);
  const mcp = useMemo(() => promptMcpFindings(cfg, intro), [cfg, intro]);
  const logical = useMemo(() => logicalPromptFindings(cfg), [cfg]);
  const effectiveRules = useMemo(() => adminRules ?? seedRules(), [adminRules]);
  const adminFindings = useMemo(() => ruleFindings(cfg, effectiveRules), [cfg, effectiveRules]);
  const tools = enabledTools(cfg);
  const mcpToolCount = (cfg.mcp_servers ?? []).reduce((n: number, s: any) => {
    const info = intro[s.url]; return n + (info && !info.error ? info.tools.filter((t) => !t.startsWith("polartie_")).length : 0);
  }, 0);
  const problems = [...struct, ...mcp, ...logical, ...adminFindings].filter((f) => f.level === "error").length;

  const stat = (label: string, value: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderBottom: "1px solid var(--line)", fontSize: 13 }}>
      <b style={{ color: "var(--text)" }}>{label}</b><span className="mono" style={{ color: "var(--good)" }}>{value}</span>
    </div>
  );

  return (
    <div className="view">
      <div className="eyebrow">Config viewer</div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead">
          <span>Robot config.json</span>
          <span className="sub" style={{ fontSize: 11 }}>· saved in this browser</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <button className="btn ghost sm" onClick={resetToExample} title="Discard the locally-saved working config and reload the example">Reset to example</button>
            <button className="btn ghost sm" onClick={() => preset(SAMPLE_CONFIG)}>Sample</button>
            {problems > 0 && <span className="chip crit"><span className="cd" />{problems} problem{problems === 1 ? "" : "s"}</span>}
            <button className="btn sm ghost" onClick={downloadRobot} disabled={renderBusy || problems > 0}
              title={problems > 0 ? "Fix the errors first — a config with problems can't be deployed" : "Resolve connector/secret references server-side and download the deploy-ready config.json"}>
              {renderBusy ? "Rendering…" : "Download robot config"}
            </button>
            <button className="btn sm primary" onClick={() => load(text)}>Load &amp; render</button>
          </span>
        </div>
        <div className="cbody">
          <textarea className="area mono" rows={7} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          {err && <div className="lintline" style={{ color: "var(--crit)", marginTop: 6 }}>JSON error: {err}</div>}
          {renderNotes && renderNotes.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {renderNotes.map((n, i) => <div key={i} className="lintline" style={{ color: "var(--warn)", fontSize: 12.5 }}>⚠ {n}</div>)}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Guided edit</span>
          <span className="sub" style={{ marginLeft: "auto" }}>edit within the admin's bounds — blocking violations can't be applied</span></div>
        <div className="cbody">
          <GuidedEditor cfg={cfg} rules={effectiveRules}
            rulesetLabel={adminRules ? `published ruleset v${adminVersion}` : "the built-in default rules"}
            onApply={(next) => { setCfg(next); setText(JSON.stringify(next, null, 2)); setLogicalLive(null); }} />
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
        <div className="chead"><span>Admin constraint rules</span>
          <span className="sub" style={{ marginLeft: "auto" }}>
            {adminRules
              ? `enforcing published ruleset v${adminVersion} · ${effectiveRules.length} rules`
              : `built-in defaults · ${effectiveRules.length} rules — publish from Config admin to override`}
          </span></div>
        <div className="cbody">
          {adminFindings.length
            ? <Findings items={adminFindings} />
            : <div className="lintline" style={{ display: "flex", gap: 9 }}><span style={{ color: "var(--good)", fontWeight: 700 }}>✔</span><span>All {effectiveRules.length} admin rules pass.</span></div>}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>MCP tools ↔ prompt</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <span className="sub">{live ? "introspected live via list_tools" : "does the prompt reflect the tools the servers actually provide?"}</span>
            <button className="btn sm ghost" onClick={introspect} disabled={busy || !(cfg.mcp_servers ?? []).length}>
              {busy ? "Introspecting…" : live ? "Re-introspect" : "Introspect live"}
            </button>
          </span></div>
        <div className="cbody">
          {introMsg && <div className="lintline" style={{ color: "var(--crit)", marginBottom: 6 }}>{introMsg}</div>}
          <Findings items={mcp} />
        </div>
      </div>

      <div className="card">
        <div className="chead"><span>Logical prompt validation</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <span className="sub">{logicalLive ? "checked by the LLM against the config" : "freeform prompt vs config · heuristic preview"}</span>
            <button className="btn sm ghost" onClick={validate} disabled={busy2}>{busy2 ? "Validating…" : "Validate (LLM)"}</button>
          </span></div>
        <div className="cbody"><Findings items={logicalLive ?? logical} /></div>
      </div>
    </div>
  );
}
