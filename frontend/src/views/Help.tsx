// In-UI help: a (?) marker with a hover/click tooltip. First-version help per
// NEW_FEATURES.md — explains non-trivial concepts near the controls they label.
// Content is a small concept glossary; schema/field help comes from the schema's
// own descriptions (shown as tooltips in the guided editor).
import { useState } from "react";

export const GLOSSARY: Record<string, string> = {
  schema: "The 'available options' for this config — the fields, tools and structures an admin declares (with types, enum values, which are required). The user stage can only edit within it.",
  rules: "Constraints the config must satisfy: requires (if A then B), conflicts (not both A and B), enum (a field's allowed values). Authored by the admin; the user stage is blocked from breaking them.",
  ruleset: "The published set of rules + schema for a project. Versioned; the published version is what the user stage enforces.",
  subsystems: "Which subsystems run per session: SOP management and/or background retrieval. Modes: SOP+retrieval (both), SOP only, Retrieval only, Advisory (low-latency, off the reply path).",
  advisory: "A low-latency mode: the responder gets the whole SOP + fresh data in one call; classification/tracking run off the reply path. Best for knowledge-delivery Q&A.",
  connector: "A named connection to a retrieval/tool system (MCP, RAG, HTTP, corpus), defined once in the registry and reused by SOPs and the agent config. Secrets stay server-side.",
  needs_input: "A Stage-0 analysis found this option exists but couldn't resolve its details (e.g. an enum's allowed values). It's flagged for Engineering to supply before it can be finalized.",
  analysis_report: "The Stage-0 discovery output (JSON): the target system's components, all config options, integrations and unknowns. Seeds the Stage-1 schema; re-runs merge in, preserving your curation.",
  "secret-ref": "A reference to a credential stored server-side (tenant secrets) — the config carries the name, never the value. Resolved at write-back.",
  write_back: "Rendering the edited config into the deploy-ready robot config.json: connector/secret references are resolved and the result is validated against the schema.",
};

export default function Help({ topic, text }: { topic?: keyof typeof GLOSSARY | string; text?: string }) {
  const [open, setOpen] = useState(false);
  const body = text ?? (topic ? GLOSSARY[topic] : "") ?? "";
  if (!body) return null;
  return (
    <span style={{ position: "relative", display: "inline-flex" }}
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" aria-label="help" onClick={() => setOpen((o) => !o)}
        style={{ width: 15, height: 15, lineHeight: "13px", fontSize: 10.5, fontWeight: 700, borderRadius: "50%",
          border: "1px solid var(--line)", background: "var(--panel2, rgba(127,127,127,.12))", color: "var(--muted)",
          cursor: "help", padding: 0, marginLeft: 5 }}>?</button>
      {open && (
        <span role="tooltip" style={{ position: "absolute", top: "120%", left: 0, zIndex: 40, width: 280,
          background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 8, padding: "9px 11px",
          fontSize: 12, lineHeight: 1.5, color: "var(--text2)", fontWeight: 400, textTransform: "none",
          letterSpacing: 0, boxShadow: "0 6px 24px rgba(0,0,0,.18)" }}>
          {body}
        </span>
      )}
    </span>
  );
}
