// Config admin — stage 1 of the config-management feature.
//
// The admin authors the constraint rules the user stage (Config.tsx) enforces:
// the "rules become content, not code" layer. Rules are held to the formal
// feature-model — enum / requires / conflicts (the design's decided scope) — and
// evaluated live against a real config so the admin sees each rule fire as they
// write it. Rule drafting is LLM-assisted; the engine stays formal.
import { useMemo, useState } from "react";
import AENA from "../config/aenaConfig.json";
import { SAMPLE_CONFIG } from "../config/sampleConfig";
import type { Config } from "../config/configModel";
import {
  describeRule, evaluateRules, ruleVocabulary, seedRules,
  type Level, type Rule, type RuleResult,
} from "../config/rules";
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

// ---- main view --------------------------------------------------------------

export default function ConfigAdminView() {
  const [rules, setRules] = useState<Rule[]>(seedRules());
  const [cfg, setCfg] = useState<Config>(AENA as Config);
  const [target, setTarget] = useState<"aena" | "sample">("aena");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [draftErr, setDraftErr] = useState("");
  const [showJson, setShowJson] = useState(false);

  const pick = (t: "aena" | "sample") => { setTarget(t); setCfg((t === "aena" ? AENA : SAMPLE_CONFIG) as Config); };
  const results = useMemo(() => evaluateRules(cfg, rules), [cfg, rules]);
  const vocab = useMemo(() => ruleVocabulary(cfg), [cfg]);
  const violated = results.filter((r) => r.state === "violated").length;

  const remove = (id: string) => setRules((rs) => rs.filter((r) => r.id !== id));
  const add = (r: Rule) => setRules((rs) => [...rs, r]);

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
      <div className="eyebrow">Config admin · stage 1</div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="chead">
          <span>Constraint rules</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <span className="sub">evaluated against</span>
            <button className={"btn ghost sm" + (target === "aena" ? " primary" : "")} onClick={() => pick("aena")}>AENA (real)</button>
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
          <span style={{ marginLeft: "auto" }}>
            <button className="btn ghost sm" onClick={() => setShowJson((s) => !s)}>{showJson ? "Hide" : "View"} ruleset JSON</button>
          </span></div>
        <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
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
