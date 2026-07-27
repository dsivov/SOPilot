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
import { configToGraph, validateConfig, promptMcpFindings, logicalPromptFindings, enabledTools, type Finding, type Introspection } from "../config/configModel";
import { ruleFindings, seedRules, type Rule } from "../config/rules";
import type { ConfigSchema } from "../config/schema";
import GuidedEditor, { applyEdits, type EditOp } from "./ConfigEdit";
import Help from "./Help";
import { editorFields } from "../config/schema";
import { useCopilotApply, useCopilotSnapshot } from "../copilot/bridge";
import { api, getCreds } from "../api";

// A fresh project starts from this empty skeleton — NOT the bundled example
// (which is a real, sanitized robot config; showing it by default made every
// new tenant look like it "already had" that customer's config). The example
// stays available behind an explicit "Load example" button.
const EMPTY_CONFIG: Record<string, any> = {
  display_name: "", voice: "", default_language_iso: "",
  tools: {}, mcp_servers: [], knowledge_base: [], transfer_topics: [],
};

// The working config is persisted per TENANT+project in this browser (keyed by
// the tenant so two tenants that both have a "main" project can't share a
// working copy). Export / "Download robot config" is the durable artifact.
const tenantTag = () => (localStorage.getItem("sopilot-tenant") || "t").slice(0, 24);
const cfgStoreKey = () => `sopilot-config:${tenantTag()}:${getCreds().project || "default"}`;
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
  const initial = loadStoredConfig() ?? EMPTY_CONFIG;
  const [text, setText] = useState(JSON.stringify(initial, null, 2));
  const [cfg, setCfg] = useState<Record<string, any>>(initial);
  const [err, setErr] = useState("");
  const [intro, setIntro] = useState<Introspection>(MCP_INTROSPECTION);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [introMsg, setIntroMsg] = useState("");
  // Admin-published constraint rules (stage-1 → user-stage handoff). null until
  // fetched; falls back to the built-in seed when nothing is published yet.
  const [adminRules, setAdminRules] = useState<Rule[] | null>(null);
  const [adminVersion, setAdminVersion] = useState<number | null>(null);
  const [adminSchema, setAdminSchema] = useState<ConfigSchema | null>(null);
  const [renderNotes, setRenderNotes] = useState<string[] | null>(null);
  const [renderBusy, setRenderBusy] = useState(false);
  // DB-versioned config document — the durable home for the config. localStorage
  // is now just an offline draft cache; the DB is the source of truth.
  const [docVersion, setDocVersion] = useState(0);
  const [docPublished, setDocPublished] = useState<number | null>(null);
  const [docBaseline, setDocBaseline] = useState<string>(JSON.stringify(initial));  // last saved config JSON
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

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
    api<{ published_version: number | null; published_rules: Rule[] | null; published_schema: ConfigSchema | null }>("GET", "/config/ruleset")
      .then((r) => {
        if (r.published_rules) { setAdminRules(r.published_rules); setAdminVersion(r.published_version); }
        if (r.published_schema) setAdminSchema(r.published_schema);
      })
      .catch(() => { /* backend down — seed fallback below */ });
    // Load the DB config document (source of truth) for versioning + dirty
    // tracking. The DB config is always the baseline, but DON'T clobber an
    // unsaved local draft: if the browser holds edits that differ from the DB
    // (e.g. "Apply changes" without "Save", then a tab switch remounted this
    // view), keep the draft and let the "unsaved" chip prompt a Save — otherwise
    // navigating away would silently discard in-progress work.
    api<{ config: any; latest_version: number; published_version: number | null }>("GET", "/config/document")
      .then((d) => {
        setDocVersion(d.latest_version); setDocPublished(d.published_version);
        if (d.config) {
          setDocBaseline(JSON.stringify(d.config));
          const local = loadStoredConfig();
          if (!local || JSON.stringify(local) === JSON.stringify(d.config)) preset(d.config);
          // else: a divergent local draft exists — leave it in place (dirty vs DB).
        }
      })
      .catch(() => { /* no document / backend down — localStorage/empty stands */ });
  }, []);

  // Persist the working config per project (offline draft cache; DB is truth).
  useEffect(() => {
    try { localStorage.setItem(cfgStoreKey(), JSON.stringify(cfg)); } catch { /* quota/serialization — non-fatal */ }
  }, [cfg]);

  const docDirty = JSON.stringify(cfg) !== docBaseline;

  // Copilot (Stage 2): publish the working config + bounds, and apply config_edits
  // the copilot proposes — re-gated by the same allow-sets the guided editor uses.
  useCopilotSnapshot({ config: cfg, published_version: docPublished, dirty: docDirty, has_schema: !!adminSchema });
  useCopilotApply((p) => {
    if (p.kind !== "config_edits") return null;
    const edits = (p.payload as { edits?: EditOp[] })?.edits;
    if (!Array.isArray(edits) || edits.length === 0) return null;
    const fields = editorFields(cfg as any, adminSchema);
    const allowedFields = new Set(fields.map((f) => f.path));
    const allowedTools = new Set(adminSchema?.tools?.length ? adminSchema.tools.map((t) => t.name) : Object.keys((cfg.tools as object) ?? {}));
    const { next, applied, skipped } = applyEdits(cfg as any, edits, allowedFields, allowedTools);
    setCfg(next as Record<string, any>);
    setText(JSON.stringify(next, null, 2));
    return `Applied ${applied.length} change(s) to the working config`
      + (skipped.length ? `, skipped ${skipped.length} out-of-schema` : "")
      + ". Review, then Save.";
  });
  const saveDocument = async (publish: boolean) => {
    setSaveBusy(true); setSaveMsg("");
    try {
      const r = await api<{ version: number; published_version: number | null }>("PUT", "/config/document", { config: cfg, publish });
      setDocVersion(r.version); setDocPublished(r.published_version); setDocBaseline(JSON.stringify(cfg));
      setSaveMsg(`Saved v${r.version}${publish ? " · published" : ""}`);
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setSaveMsg(m.includes("Not Found") ? "Save endpoint not found — restart the backend for /config/document." : `Save failed: ${m}`);
    } finally { setSaveBusy(false); }
  };

  const load = (v: string) => { try { setCfg(JSON.parse(v)); setErr(""); } catch (e: any) { setErr(String(e?.message ?? e)); } };
  const preset = (c: any) => { setText(JSON.stringify(c, null, 2)); setCfg(c); setErr(""); setIntro(MCP_INTROSPECTION); setLive(false); };
  const resetEmpty = () => { try { localStorage.removeItem(cfgStoreKey()); } catch { /* ignore */ } preset(EMPTY_CONFIG); };
  // Throw away the local draft and reload the last-saved config from the DB.
  const discardToSaved = async () => {
    try {
      const d = await api<{ config: any; latest_version: number; published_version: number | null }>("GET", "/config/document");
      setDocVersion(d.latest_version); setDocPublished(d.published_version);
      setDocBaseline(JSON.stringify(d.config ?? EMPTY_CONFIG));
      preset(d.config ?? EMPTY_CONFIG);
      setSaveMsg("");
    } catch (e: any) { setSaveMsg(`Reload failed: ${String(e?.message ?? e)}`); }
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
  const isEmptyConfig = !String(cfg.display_name || "").trim() && tools.length === 0
    && !(cfg.mcp_servers ?? []).length && !(cfg.knowledge_base ?? []).length
    && !(cfg.transfer_topics ?? []).length && !String(cfg.prompt || "").trim();

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
          {docVersion > 0 && (
            <span className={"chip " + (docPublished === docVersion && !docDirty ? "good" : "muted")} style={{ marginLeft: 6 }}>
              <span className="cd" />v{docVersion}{docPublished ? (docPublished === docVersion ? " published" : ` · v${docPublished} deployed`) : " draft"}
            </span>
          )}
          {docDirty && <span className="chip warn" style={{ marginLeft: 4 }}><span className="cd" />unsaved</span>}
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <button className="btn ghost sm" onClick={() => preset(EXAMPLE)} title="Load the bundled example (a sanitized real robot config) into this working copy">Load example</button>
            <button className="btn ghost sm" onClick={() => preset(SAMPLE_CONFIG)}>Sample</button>
            <button className="btn ghost sm" onClick={resetEmpty} title="Clear back to an empty config">Reset</button>
            {docDirty && docVersion > 0 && (
              <button className="btn ghost sm" onClick={discardToSaved} title="Discard unsaved local changes and reload the last-saved config from the database">Discard changes</button>
            )}
            <button className="btn ghost sm" onClick={() => saveDocument(false)} disabled={saveBusy || !docDirty} title="Save this config as a new version in the database">Save</button>
            <button className="btn sm primary" onClick={() => saveDocument(true)} disabled={saveBusy || problems > 0} title={problems > 0 ? "Fix errors before publishing" : "Save and mark as the deploy version"}>Save &amp; publish</button>
            <button className="btn sm ghost" onClick={downloadRobot} disabled={renderBusy || problems > 0}
              title={problems > 0 ? "Fix the errors first — a config with problems can't be deployed" : "Resolve connector/secret references server-side and download the deploy-ready config.json"}>
              {renderBusy ? "Rendering…" : "Download"}
            </button>
            <button className="btn ghost sm" onClick={() => load(text)}>Load &amp; render</button>
          </span>
        </div>
        <div className="cbody">
          {isEmptyConfig && (
            <div style={{ marginBottom: 8, padding: "8px 12px", background: "var(--panel2, rgba(127,127,127,.08))", border: "1px solid var(--line)", borderRadius: 8, fontSize: 12.5, color: "var(--muted)" }}>
              No working config in this browser yet — this is a local scratch copy, not your SOPs/blocks/connectors (those live on the server and are unaffected).
              Start with <b>Load example</b>, <b>Import</b> a config JSON, or paste one below.
            </div>
          )}
          <textarea className="area mono" rows={7} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          {err && <div className="lintline" style={{ color: "var(--crit)", marginTop: 6 }}>JSON error: {err}</div>}
          {saveMsg && <div className="lintline" style={{ color: saveMsg.startsWith("Saved") ? "var(--good)" : "var(--crit)", marginTop: 6 }}>{saveMsg}</div>}
          {renderNotes && renderNotes.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {renderNotes.map((n, i) => <div key={i} className="lintline" style={{ color: "var(--warn)", fontSize: 12.5 }}>⚠ {n}</div>)}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Guided edit<Help topic="write_back" text="Edit the config within the admin's published schema & rules. Changes are staged here; Apply changes writes them to the config. Blocking rule violations can't be applied." /></span>
          <span className="sub" style={{ marginLeft: "auto" }}>edit within the admin's bounds — blocking violations can't be applied</span></div>
        <div className="cbody">
          <GuidedEditor cfg={cfg} rules={effectiveRules} schema={adminSchema}
            rulesetLabel={adminRules ? `published ruleset v${adminVersion}` : "the built-in default rules"}
            onApply={(next) => { setCfg(next); setText(JSON.stringify(next, null, 2)); }} />
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
            <span className="sub">freeform prompt vs config · heuristic preview — ask the copilot for a deeper check</span>
          </span></div>
        <div className="cbody"><Findings items={logical} /></div>
      </div>
    </div>
  );
}
