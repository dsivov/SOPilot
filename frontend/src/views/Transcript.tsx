// Feature E: full per-turn transcript of a real session — conversation,
// SOP instructions/prompts actually served, retrieval data, timings.
// Used by the Autopilot A/B drill-down; data comes from /journey + /fetches.
import { useEffect, useState } from "react";
import { api } from "../api";

type Debug = {
  prompt_text?: string; context_block?: string; stage_blocks?: string[];
  allowed_actions?: string[]; retrieval?: Record<string, string>;
  rerank_ms?: number; respond_ms?: number; reply_source?: string;
  consume_stats?: { consumed?: number; live?: number };
} | null;
type JTurn = {
  turn_index: number; user_message: string; assistant_message: string; state: string; action: string;
  mood: string; instruction_hit: boolean; duration_ms: number; debug: Debug;
};
type FetchRow = {
  kind: string; connector?: string; dependency_name: string; predictor_source: string;
  consumed: boolean; wasted: boolean; fetch_duration_ms: number;
  issued_at_turn: number; consumed_at_turn: number | null; payload_summary: string; error: boolean;
};

function Mono({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ fontSize: 12 }}>
      <button className="btn ghost sm" onClick={() => setOpen(!open)} style={{ padding: "1px 8px", fontSize: 11 }}>
        {open ? "▾" : "▸"} {label}
      </button>
      {open && (
        <pre style={{ margin: "6px 0 0", whiteSpace: "pre-wrap", fontSize: 11.5, lineHeight: 1.5, maxHeight: 220, overflow: "auto", background: "var(--surface2)", borderRadius: 8, padding: "8px 10px", color: "var(--text2)" }}>
          {text}
        </pre>
      )}
    </div>
  );
}

export default function Transcript({ sessionId }: { sessionId: string }) {
  const [turns, setTurns] = useState<JTurn[] | null>(null);
  const [fetches, setFetches] = useState<FetchRow[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    setTurns(null);
    setErr("");
    api<{ turns: JTurn[] }>("GET", `/sessions/${sessionId}/journey`)
      .then((j) => setTurns(j.turns))
      .catch((e) => setErr(String(e)));
    api<{ fetches: FetchRow[] }>("GET", `/sessions/${sessionId}/fetches`)
      .then((f) => setFetches(f.fetches))
      .catch(() => {});
  }, [sessionId]);

  if (err) return <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{err}</span>;
  if (!turns) return <div className="spin" />;
  if (turns.length === 0) return <div className="empty">No turns recorded for this session.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {turns.map((t) => {
        const served = fetches.filter((f) => f.consumed_at_turn === t.turn_index && f.kind !== "instruction");
        const d = t.debug;
        return (
          <div key={t.turn_index} style={{ border: "1px solid var(--line)", borderRadius: 10, padding: "10px 12px" }}>
            <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 6 }}>
              <span className="chip"><span className="cd" />turn {t.turn_index}</span>
              {t.action && <span className="chip comm"><span className="cd" />{t.action}</span>}
              {t.state && <span className="chip accent"><span className="cd" />{t.state}</span>}
              {d?.reply_source && (
                <span className={"chip " + (d.reply_source === "pre-draft" ? "good" : "")}>
                  <span className="cd" />reply: {d.reply_source}
                </span>
              )}
              <span className="chip"><span className="cd" />{t.duration_ms} ms{d?.respond_ms !== undefined ? ` (respond ${d.respond_ms})` : ""}</span>
            </div>
            <div style={{ fontSize: 13, marginBottom: 3 }}><b>User:</b> {t.user_message || <i style={{ color: "var(--muted)" }}>(none)</i>}</div>
            <div style={{ fontSize: 13, marginBottom: 6 }}><b>Agent:</b> {t.assistant_message || <i style={{ color: "var(--muted)" }}>(none)</i>}</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {d?.prompt_text && <Mono label="SOP instructions / prompt served to the agent" text={d.prompt_text} />}
              {!d?.prompt_text && d?.context_block && <Mono label="context block (retrieval-only mode)" text={d.context_block} />}
              {d?.stage_blocks && d.stage_blocks.length > 0 && <Mono label={`stage prompt blocks (${d.stage_blocks.length})`} text={d.stage_blocks.join("\n---\n")} />}
              {d?.retrieval && Object.keys(d.retrieval).length > 0 && (
                <Mono
                  label={`retrieval data used (${Object.keys(d.retrieval).length})`}
                  text={Object.entries(d.retrieval).map(([k, v]) => `${k}:\n${v}`).join("\n\n")}
                />
              )}
              {served.length > 0 && (
                <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                  {served.map((f, i) => (
                    <span key={i} className="chip" title={f.payload_summary}>
                      <span className="cd" />
                      {f.dependency_name}{f.connector ? ` @${f.connector}` : ""} · {f.fetch_duration_ms} ms
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
