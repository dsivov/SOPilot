// Guided config editing — stage 2 of the config-management feature.
//
// The user edits the config INSIDE the bounds the admin published: every change
// is re-evaluated against the published ruleset live; enum rules become the
// widget itself (the admin's options are the user's dropdown); violated rules
// explain themselves and offer one-click fixes derived from the rule; and a
// config with error-level violations cannot be applied at all. The engine stays
// formal — guidance comes from the rules, not from heuristics.
import { useEffect, useMemo, useState } from "react";
import type { Config } from "../config/configModel";
import { describePredicate, evaluateRules, type Rule, type RuleResult } from "../config/rules";

// Scalar dot-path fields offered for editing (the rule vocabulary's editable
// subset — complex structures stay in the JSON textarea for now).
const EDIT_FIELDS = [
  "voice", "default_language_iso", "notification_service_url",
  "opensearch_endpoint", "lightrag.postgres.host", "custom_config.gpt_model",
];

function get(cfg: any, path: string): any {
  return path.split(".").reduce<any>((o, k) => (o == null ? undefined : o[k]), cfg);
}
function setPath(cfg: any, path: string, value: any): any {
  const next = structuredClone(cfg);
  const keys = path.split(".");
  let o = next;
  for (const k of keys.slice(0, -1)) o = o[k] ?? (o[k] = {});
  const last = keys[keys.length - 1];
  if (value === "") delete o[last]; else o[last] = value;
  return next;
}

// tool:a|b → ["a","b"]; anything else → null
const toolsOf = (pred: string): string[] | null =>
  pred.startsWith("tool:") ? pred.slice(5).split("|").map((s) => s.trim()) : null;
const fieldOf = (pred: string): string | null => (pred.startsWith("field:") ? pred.slice(6) : null);

// One-click fixes derived from a violated rule — the "guided" part.
interface Fix { label: string; apply: (draft: Config) => Config }
function fixesFor(res: RuleResult, draft: Config): Fix[] {
  const r = res.rule;
  const disable = (names: string[]): Fix[] =>
    names.filter((n) => draft.tools?.[n]?.enabled).map((n) => ({
      label: `Disable ${n}`,
      apply: (d) => setPath(d, `tools.${n}.enabled`, false),
    }));
  if (r.kind === "requires") return disable(toolsOf(r.when) ?? []);
  if (r.kind === "conflicts") return [...disable(toolsOf(r.a) ?? []), ...disable(toolsOf(r.b) ?? [])];
  return r.options.length ? [{ label: `Set ${r.field} = "${r.options[0]}"`, apply: (d) => setPath(d, r.field, r.options[0]) }] : [];
}

export default function GuidedEditor({ cfg, rules, rulesetLabel, onApply }: {
  cfg: Config; rules: Rule[]; rulesetLabel: string; onApply: (next: Config) => void;
}) {
  const [draft, setDraft] = useState<Config>(() => structuredClone(cfg));
  useEffect(() => { setDraft(structuredClone(cfg)); }, [cfg]);

  const results = useMemo(() => evaluateRules(draft, rules), [draft, rules]);
  const violated = results.filter((r) => r.state === "violated");
  const errors = violated.filter((r) => r.rule.level === "error");
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(cfg), [draft, cfg]);

  // Enum rules drive their field's widget: the admin's options ARE the choices.
  const enumFor = (field: string) => rules.find((r): r is Extract<Rule, { kind: "enum" }> => r.kind === "enum" && r.field === field);
  // Fields a violated requires-rule needs — highlighted so the user sees where to act.
  const neededFields = new Set(violated.flatMap((v) => v.rule.kind === "requires" ? [fieldOf(v.rule.needs)].filter(Boolean) : []));

  const toggleTool = (name: string) => setDraft((d) => setPath(d, `tools.${name}.enabled`, !d.tools?.[name]?.enabled));
  const toolNames = Object.keys(draft.tools ?? {}).sort();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className="sub">Every edit is checked against <b style={{ color: "var(--text2)" }}>{rulesetLabel}</b> before it can be applied.</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          {errors.length > 0
            ? <span className="chip crit"><span className="cd" />{errors.length} blocking</span>
            : violated.length > 0
              ? <span className="chip warn"><span className="cd" />{violated.length} warning{violated.length === 1 ? "" : "s"}</span>
              : <span className="chip good"><span className="cd" />within bounds</span>}
          <button className="btn ghost sm" onClick={() => setDraft(structuredClone(cfg))} disabled={!dirty}>Reset</button>
          <button className="btn sm primary" onClick={() => onApply(draft)} disabled={!dirty || errors.length > 0}
            title={errors.length ? "Fix the blocking violations first — the admin ruleset forbids this config." : ""}>
            Apply changes
          </button>
        </span>
      </div>

      {/* the violations panel IS the guidance: each violated rule explains itself and offers derived fixes */}
      {violated.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "8px 10px", background: "var(--panel2, rgba(127,127,127,.06))", borderRadius: 8 }}>
          {violated.map((res) => (
            <div key={res.rule.id} className="lintline" style={{ display: "flex", gap: 9, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ color: res.rule.level === "error" ? "var(--crit)" : "var(--warn)", fontWeight: 700 }}>
                {res.rule.level === "error" ? "✖" : "⚠"}
              </span>
              <span style={{ flex: 1, minWidth: 200 }}>{res.rule.msg}</span>
              {fixesFor(res, draft).map((f) => (
                <button key={f.label} className="btn ghost sm" onClick={() => setDraft(f.apply(draft))}>{f.label}</button>
              ))}
            </div>
          ))}
        </div>
      )}

      <div className="grid2">
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            Tools ({toolNames.filter((t) => draft.tools?.[t]?.enabled).length} enabled)
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {toolNames.map((t) => {
              const on = draft.tools?.[t]?.enabled === true;
              return (
                <button key={t} className={"chip " + (on ? "accent" : "muted")} onClick={() => toggleTool(t)}
                  style={{ cursor: "pointer", opacity: on ? 1 : 0.6 }} title={on ? "Click to disable" : "Click to enable"}>
                  <span className="cd" />{t}
                </button>
              );
            })}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
            Fields
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {EDIT_FIELDS.map((f) => {
              const en = enumFor(f);
              const v = get(draft, f);
              const needed = neededFields.has(f);
              return (
                <label key={f} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12 }}>
                  <span className="mono" style={{ flex: "0 0 190px", color: needed ? "var(--crit)" : "var(--muted)" }}>
                    {f}{needed ? " ←" : ""}
                  </span>
                  {en ? (
                    // the admin's enum rule bounds the widget itself
                    <select className="area mono" style={{ flex: 1, padding: "4px 8px" }} value={String(v ?? "")}
                      onChange={(e) => setDraft(setPath(draft, f, e.target.value))}>
                      {!en.options.includes(String(v ?? "")) && <option value={String(v ?? "")}>{String(v ?? "(unset)")} — not allowed</option>}
                      {en.options.map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input className="area mono" style={{ flex: 1, padding: "4px 8px", borderColor: needed ? "var(--crit)" : undefined }}
                      value={typeof v === "string" ? v : v == null ? "" : JSON.stringify(v)}
                      placeholder="(unset)"
                      onChange={(e) => setDraft(setPath(draft, f, e.target.value))} />
                  )}
                </label>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
