// Config admin — stage 1 of the config-management feature.
//
// The admin authors the constraint rules the user stage (Config.tsx) enforces:
// the "rules become content, not code" layer. Rules are held to the formal
// feature-model — enum / requires / conflicts (the design's decided scope) — and
// evaluated live against a real config so the admin sees each rule fire as they
// write it. Rule drafting is LLM-assisted; the engine stays formal.
import { useEffect, useMemo, useState } from "react";
import EXAMPLE from "../config/exampleConfig.json";
import { SAMPLE_CONFIG } from "../config/sampleConfig";
import type { Config } from "../config/configModel";
import {
  describeRule, evaluateRules, ruleVocabulary, seedRules,
  type Level, type Rule, type RuleResult,
} from "../config/rules";
import { schemaFromConfig, type ConfigSchema, type SchemaFieldDef } from "../config/schema";
import type { FieldType } from "../config/configVocab";
import { api } from "../api";

const KIND_LABEL: Record<Rule["kind"], string> = { requires: "requires", conflicts: "conflicts", enum: "enum" };
const STATE_STYLE: Record<RuleResult["state"], { label: string; cls: string }> = {
  violated: { label: "violated", cls: "crit" },
  satisfied: { label: "satisfied", cls: "good" },
  inactive: { label: "inactive", cls: "muted" },
};

let _seq = 0;
const newId = (kind: string) => `${kind}-${(_seq++).toString(36)}${Date.now().toString(36).slice(-4)}`;

// ---- add-rule form ----------------------------------------------------------

function AddRule({ onAdd }: { onAdd: (r: Rule) => void }) {
  const [kind, setKind] = useState<Rule["kind"]>("requires");
  const [level, setLevel] = useState<Level>("error");
  const [msg, setMsg] = useState("");
  const [p1, setP1] = useState(""); // when / a / field
  const [p2, setP2] = useState(""); // needs / b / options(csv)

  const add = () => {
    const base = { id: newId(kind), level, msg: msg.trim() || describePlaceholder() };
    let r: Rule;
    if (kind === "requires") r = { ...base, kind, when: p1.trim(), needs: p2.trim() };
    else if (kind === "conflicts") r = { ...base, kind, a: p1.trim(), b: p2.trim() };
    else r = { ...base, kind, field: p1.trim(), options: p2.split(",").map((s) => s.trim()).filter(Boolean) };
    onAdd(r);
    setMsg(""); setP1(""); setP2("");
  };
  const describePlaceholder = () => (kind === "enum" ? `${p1 || "field"} must be one of the allowed options` : "describe what a violation means");
  const valid = kind === "enum" ? p1.trim() && p2.trim() : p1.trim() && p2.trim();
  const [lbl1, lbl2, hint1, hint2] =
    kind === "requires" ? ["when", "needs", "tool:send_email · field:… · kb_mode:…", "field:notification_service_url"]
    : kind === "conflicts" ? ["a", "b", "tool:knowledge_base_query", "tool:knowledge_base_query_lightrag"]
    : ["field", "options (comma-sep)", "voice", "alloy, echo, shimmer"];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select className="area mono" style={{ width: "auto", padding: "4px 8px" }} value={kind} onChange={(e) => setKind(e.target.value as Rule["kind"])}>
          {(["requires", "conflicts", "enum"] as const).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
        </select>
        <select className="area mono" style={{ width: "auto", padding: "4px 8px" }} value={level} onChange={(e) => setLevel(e.target.value as Level)}>
          <option value="error">error</option><option value="warn">warn</option>
        </select>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <label style={{ flex: 1, minWidth: 200, fontSize: 12, color: "var(--muted)" }}>{lbl1}
          <input className="area mono" style={{ marginTop: 3 }} placeholder={hint1} value={p1} onChange={(e) => setP1(e.target.value)} />
        </label>
        <label style={{ flex: 1, minWidth: 200, fontSize: 12, color: "var(--muted)" }}>{lbl2}
          <input className="area mono" style={{ marginTop: 3 }} placeholder={hint2} value={p2} onChange={(e) => setP2(e.target.value)} />
        </label>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input className="area" style={{ flex: 1 }} placeholder="violation message (what the user sees)" value={msg} onChange={(e) => setMsg(e.target.value)} />
        <button className="btn sm primary" onClick={add} disabled={!valid}>Add rule</button>
      </div>
    </div>
  );
}

// ---- schema (DSL) authoring -------------------------------------------------

const FIELD_TYPES: FieldType[] = ["string", "number", "boolean", "enum", "secret-ref", "connector-ref"];

// Declares WHICH config options exist and their type-level shape (the DSL). The
// user stage draws its fields/widgets from this when published; rules layer on top.
function SchemaEditor({ schema, cfg, onChange }: { schema: ConfigSchema | null; cfg: Config; onChange: (s: ConfigSchema | null) => void }) {
  const fields = schema?.fields ?? [];
  const setFields = (fs: SchemaFieldDef[]) => onChange({ ...(schema ?? { fields: [] }), fields: fs });
  const patch = (i: number, k: keyof SchemaFieldDef, v: any) => setFields(fields.map((f, j) => (j === i ? { ...f, [k]: v } : f)));
  // LLM-assisted field authoring: plain English → one SchemaFieldDef.
  const [ask, setAsk] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [askErr, setAskErr] = useState("");
  const draftField = async () => {
    if (!ask.trim()) return;
    setAskBusy(true); setAskErr("");
    try {
      const r = await api<{ field?: SchemaFieldDef; error?: string }>("POST", "/config/draft-field", {
        instruction: ask,
        existing_fields: fields.map((f) => f.path),
        known_paths: Object.keys(cfg).concat(Object.keys(cfg.custom_config ?? {}).map((k) => `custom_config.${k}`)),
      });
      if (r.field?.path) { setFields([...fields, r.field]); setAsk(""); }
      else setAskErr(r.error || "The model did not return a valid field.");
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setAskErr(m.includes("Not Found") ? "Draft endpoint not found — restart the backend for /config/draft-field." : `Drafting failed: ${m}`);
    } finally { setAskBusy(false); }
  };
  const drafter = (
    <div style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--line)" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input className="area" style={{ flex: 1 }} placeholder='describe a field — e.g. "a weather provider, enum of openweather / tomorrow.io, required"'
          value={ask} onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !askBusy && draftField()} />
        <button className="btn sm primary" onClick={draftField} disabled={askBusy || !ask.trim()}>{askBusy ? "Drafting…" : "Draft field"}</button>
      </div>
      {askErr && <div className="lintline" style={{ color: "var(--crit)", marginTop: 6 }}>{askErr}</div>}
    </div>
  );

  if (schema === null) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div className="sub" style={{ fontSize: 12.5 }}>
          No schema yet — the user stage falls back to walking the loaded config. Declare a schema to make the
          available options authoritative (types, enums, required, descriptions), independent of any example.
        </div>
        <div>
          <button className="btn sm primary" onClick={() => onChange(schemaFromConfig(cfg))}>
            Bootstrap from the loaded config
          </button>
        </div>
        {drafter}
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {fields.length === 0 && <div className="empty" style={{ padding: "4px 0" }}>No fields — add one, or bootstrap from a config.</div>}
      {fields.map((f, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", borderBottom: "1px solid var(--line)", padding: "4px 0" }}>
          <input className="area mono" style={{ flex: "1 1 180px", padding: "3px 7px", fontSize: 12 }} placeholder="dot.path"
            value={f.path} onChange={(e) => patch(i, "path", e.target.value)} />
          <select className="area mono" style={{ width: "auto", padding: "3px 7px", fontSize: 12 }} value={f.type}
            onChange={(e) => patch(i, "type", e.target.value)}>
            {FIELD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          {f.type === "enum" && (
            <input className="area mono" style={{ flex: "1 1 140px", padding: "3px 7px", fontSize: 12 }} placeholder="options (comma-sep)"
              value={(f.options ?? []).join(", ")} onChange={(e) => patch(i, "options", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
          )}
          <input className="area" style={{ flex: "2 1 200px", padding: "3px 7px", fontSize: 12 }} placeholder="description (help shown to the user)"
            value={f.description ?? ""} onChange={(e) => patch(i, "description", e.target.value)} />
          <label className="sub" style={{ fontSize: 11.5, display: "flex", gap: 3, alignItems: "center" }} title="Must be set">
            <input type="checkbox" checked={!!f.required} onChange={(e) => patch(i, "required", e.target.checked)} />req
          </label>
          <label className="sub" style={{ fontSize: 11.5, display: "flex", gap: 3, alignItems: "center" }} title="Hidden behind the advanced toggle">
            <input type="checkbox" checked={!!f.advanced} onChange={(e) => patch(i, "advanced", e.target.checked)} />adv
          </label>
          <button className="btn ghost sm" title="Remove field" onClick={() => setFields(fields.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
        <button className="btn ghost sm" onClick={() => setFields([...fields, { path: "", type: "string" }])}>+ Add field</button>
        <button className="btn ghost sm" onClick={() => onChange(schemaFromConfig(cfg))} title="Re-seed from the loaded config (merges nothing — replaces)">Re-bootstrap</button>
        <span className="sub" style={{ flex: 1, textAlign: "right" }}>{fields.length} field{fields.length === 1 ? "" : "s"} declared</span>
        <button className="btn ghost sm" onClick={() => onChange(null)} title="Drop the schema — user stage reverts to config-derived fields">Clear schema</button>
      </div>
      {drafter}
    </div>
  );
}

// ---- main view --------------------------------------------------------------

interface RulesetInfo { exists: boolean; latest_version: number; published_version: number | null; rules: Rule[] | null; published_rules: Rule[] | null; schema: ConfigSchema | null }

export default function ConfigAdminView() {
  const [rules, setRules] = useState<Rule[]>(seedRules());
  const [cfg, setCfg] = useState<Config>(EXAMPLE as Config);
  const [target, setTarget] = useState<"example" | "sample">("example");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [draftErr, setDraftErr] = useState("");
  const [showJson, setShowJson] = useState(false);
  // The Stage-1 SCHEMA (DSL): null until authored. When set, it (not the loaded
  // config) is the field vocabulary the user stage and rule authoring use.
  const [schema, setSchema] = useState<ConfigSchema | null>(null);
  // Persistence (SopVersion-style): every save is a new immutable version; the
  // published version is what the user stage (Config view) enforces.
  const [version, setVersion] = useState(0);
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveErr, setSaveErr] = useState("");

  useEffect(() => {
    api<RulesetInfo>("GET", "/config/ruleset").then((r) => {
      if (r.exists && r.rules) { setRules(r.rules); setDirty(false); }
      if (r.schema) setSchema(r.schema);
      setVersion(r.latest_version); setPublishedVersion(r.published_version);
    }).catch(() => { /* backend down / pre-migration — stay on the seed */ });
  }, []);

  const pick = (t: "example" | "sample") => { setTarget(t); setCfg((t === "example" ? EXAMPLE : SAMPLE_CONFIG) as Config); };
  const results = useMemo(() => evaluateRules(cfg, rules), [cfg, rules]);
  // Rule authoring references the SCHEMA's field paths when a schema exists,
  // else the config-derived vocabulary.
  const vocab = useMemo(() => {
    const base = ruleVocabulary(cfg);
    return schema?.fields?.length ? { ...base, fields: schema.fields.map((f) => f.path) } : base;
  }, [cfg, schema]);
  const violated = results.filter((r) => r.state === "violated").length;

  const remove = (id: string) => { setRules((rs) => rs.filter((r) => r.id !== id)); setDirty(true); };
  const add = (r: Rule) => { setRules((rs) => [...rs, r]); setDirty(true); };

  const save = async (publish: boolean) => {
    setSaveBusy(true); setSaveErr("");
    try {
      const r = await api<{ version: number; published_version: number | null }>("PUT", "/config/ruleset", { rules, schema, publish });
      setVersion(r.version); setPublishedVersion(r.published_version); setDirty(false);
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setSaveErr(m.includes("Not Found") ? "Ruleset endpoint not found — restart the backend for /config/ruleset." : `Save failed: ${m}`);
    } finally { setSaveBusy(false); }
  };

  const draftRule = async () => {
    if (!draft.trim()) return;
    setBusy(true); setDraftErr("");
    try {
      const r = await api<{ rule?: Rule; error?: string }>("POST", "/config/draft-rule", {
        instruction: draft, tools: vocab.tools, fields: vocab.fields,
      });
      if (r.rule && r.rule.kind) { add({ ...r.rule, id: newId(r.rule.kind) }); setDraft(""); }
      else setDraftErr(r.error || "The model did not return a valid rule.");
    } catch (e: any) {
      const m = String(e?.message ?? e);
      setDraftErr(m.includes("Not Found") ? "Draft endpoint not found — restart the backend for /config/draft-rule." : `Drafting failed: ${m}`);
    } finally { setBusy(false); }
  };

  return (
    <div className="view">
      <div className="eyebrow">Config admin</div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead">
          <span>Constraint rules</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <span className="sub">evaluated against</span>
            <button className={"btn ghost sm" + (target === "example" ? " primary" : "")} onClick={() => pick("example")}>Example (real)</button>
            <button className={"btn ghost sm" + (target === "sample" ? " primary" : "")} onClick={() => pick("sample")}>Sample</button>
            {violated > 0
              ? <span className="chip crit"><span className="cd" />{violated} violated</span>
              : <span className="chip good"><span className="cd" />all pass</span>}
          </span>
        </div>
        <div className="cbody" style={{ fontSize: 12.5, color: "var(--muted)" }}>
          The rules the user stage enforces — authored here as data (<b>enum · requires · conflicts</b>), not baked into
          code. Each row shows how it evaluates against <b style={{ color: "var(--text2)" }}>{cfg.display_name || target}</b> right now.
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Rules ({rules.length})</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            {version > 0 && (
              <span className={"chip " + (publishedVersion === version && !dirty ? "good" : "muted")}>
                <span className="cd" />
                v{version}{publishedVersion ? (publishedVersion === version ? " published" : ` · v${publishedVersion} live`) : " draft"}
              </span>
            )}
            {dirty && <span className="chip warn"><span className="cd" />unsaved</span>}
            <button className="btn ghost sm" onClick={() => setShowJson((s) => !s)}>{showJson ? "Hide" : "View"} ruleset JSON</button>
            <button className="btn ghost sm" onClick={() => save(false)} disabled={saveBusy}>{saveBusy ? "Saving…" : "Save draft"}</button>
            <button className="btn sm primary" onClick={() => save(true)} disabled={saveBusy}>Save &amp; publish</button>
          </span></div>
        <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {saveErr && <div className="lintline" style={{ color: "var(--crit)" }}>{saveErr}</div>}
          {results.map((res) => {
            const s = STATE_STYLE[res.state];
            return (
              <div key={res.rule.id} className="lintline" style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--muted)", flex: "0 0 74px" }}>{KIND_LABEL[res.rule.kind]}</span>
                <span className={"chip " + s.cls} style={{ flex: "0 0 auto" }}><span className="cd" />{s.label}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <div className="mono" style={{ fontSize: 11.5, color: "var(--text2)" }}>{describeRule(res.rule)}</div>
                  <div style={{ fontSize: 12, color: res.state === "violated" ? "var(--crit)" : "var(--muted)" }}>
                    {res.state === "violated" ? res.rule.msg : res.msg}
                  </div>
                </span>
                <button className="btn ghost sm" title="Remove rule" onClick={() => remove(res.rule.id)}>✕</button>
              </div>
            );
          })}
          {!rules.length && <div className="empty">No rules yet — author one below.</div>}
          {showJson && (
            <textarea className="area mono" rows={10} readOnly value={JSON.stringify(rules, null, 2)}
              style={{ marginTop: 8 }} onFocus={(e) => e.currentTarget.select()} />
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead"><span>Available options — schema (DSL)</span>
          <span className="sub" style={{ marginLeft: "auto" }}>
            {schema?.fields?.length ? `${schema.fields.length} fields declared — the user stage uses these` : "not declared — user stage derives fields from the config"}
          </span></div>
        <div className="cbody">
          <SchemaEditor schema={schema} cfg={cfg} onChange={(s) => { setSchema(s); setDirty(true); }} />
        </div>
      </div>

      <div className="grid2">
        <div className="card">
          <div className="chead"><span>Author a rule</span></div>
          <div className="cbody"><AddRule onAdd={add} /></div>
        </div>
        <div className="card">
          <div className="chead"><span>Draft with the LLM</span>
            <span className="sub" style={{ marginLeft: "auto" }}>plain English → one structured rule</span></div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <textarea className="area" rows={3} placeholder='e.g. "if the agent can send email it must have a notification service configured"'
              value={draft} onChange={(e) => setDraft(e.target.value)} />
            {draftErr && <div className="lintline" style={{ color: "var(--crit)" }}>{draftErr}</div>}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className="sub" style={{ flex: 1 }}>{vocab.tools.length} tools · {vocab.fields.length} fields in scope</span>
              <button className="btn sm primary" onClick={draftRule} disabled={busy || !draft.trim()}>{busy ? "Drafting…" : "Draft rule"}</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
