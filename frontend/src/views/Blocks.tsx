import { BadgeCheck, Check, Save, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type BlockMeta = { name: string; kind: string; latest_version: number; latest_status: string; updated_at: string };

const KIND_TONE: Record<string, string> = { compliance: "warn", role: "accent", stage: "comm", escalation: "crit" };

export default function BlocksView() {
  const [blocks, setBlocks] = useState<BlockMeta[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("stage");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [aiInstr, setAiInstr] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [proposal, setProposal] = useState<{ content: string; notes: string } | null>(null);

  const rewrite = async () => {
    setAiBusy(true);
    setNote("");
    try {
      setProposal(await api<{ content: string; notes: string }>("POST", "/prompt-blocks/rewrite", { content, instruction: aiInstr, kind }));
    } catch (e) {
      setNote(String(e));
    } finally {
      setAiBusy(false);
    }
  };

  const refresh = useCallback(async () => setBlocks(await api<BlockMeta[]>("GET", "/prompt-blocks")), []);
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const open = async (meta: BlockMeta) => {
    const full = await api("GET", `/prompt-blocks/${encodeURIComponent(meta.name)}`);
    setName(full.name);
    setKind(full.kind);
    setContent(full.versions[0]?.content ?? "");
    setNote("");
  };

  const save = async (publish: boolean) => {
    setBusy(true);
    try {
      const r = await api("POST", "/prompt-blocks", { name, kind, content });
      if (publish) await api("POST", `/prompt-blocks/${encodeURIComponent(name)}/publish`);
      setNote(`saved v${r.version}${publish ? " and published" : " (draft)"} — running sessions keep their pinned version`);
      await refresh();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Studio</div>
          <h1>Prompt blocks</h1>
          <p>Approved wording, versioned separately from procedures. Sessions pin versions at start.</p>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <div className="chead">
            <h3>Library</h3>
            <span className="sub num">{blocks.length} blocks</span>
          </div>
          <div className="cbody" style={{ padding: 0 }}>
            {blocks.length === 0 ? (
              <div className="empty">No blocks yet — create the first on the right.</div>
            ) : (
              <div className="tablewrap" style={{ border: 0, borderRadius: 0 }}>
                <table className="table">
                  <thead>
                    <tr><th>Name</th><th>Kind</th><th>Version</th><th>Status</th><th></th></tr>
                  </thead>
                  <tbody>
                    {blocks.map((b) => (
                      <tr key={b.name} onClick={() => open(b)} style={{ cursor: "pointer" }}>
                        <td className="mono" style={{ fontSize: 12.5 }}>{b.name}</td>
                        <td><span className={"chip " + (KIND_TONE[b.kind] ?? "")}>{b.kind}</span></td>
                        <td className="mono num">v{b.latest_version}</td>
                        <td>
                          <span className={"st " + (b.latest_status === "published" ? "good" : "warn")}>
                            {b.latest_status}
                          </span>
                        </td>
                        <td style={{ width: 34 }}>
                          <button
                            className="btn ghost sm"
                            title={`Delete ${b.name}`}
                            onClick={async (e) => {
                              e.stopPropagation();
                              if (!window.confirm(`Delete prompt block “${b.name}”? SOPs binding it will fail their next publish until rebound.`)) return;
                              await api("DELETE", `/prompt-blocks/${encodeURIComponent(b.name)}`);
                              if (name === b.name) { setName(""); setContent(""); }
                              await refresh();
                            }}
                          >
                            <Trash2 size={14} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
        <div className="card">
          <div className="chead"><h3>Editor</h3></div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <input className="qinput mono" placeholder="name, e.g. compliance.recording" value={name} onChange={(e) => setName(e.target.value)} />
            <select className="qinput" value={kind} onChange={(e) => setKind(e.target.value)} style={{ flex: "none" }}>
              <option value="stage">stage</option>
              <option value="compliance">compliance</option>
              <option value="role">role</option>
              <option value="escalation">escalation</option>
            </select>
            <textarea className="area" rows={9} placeholder="The exact wording the agent must carry for this block…" value={content} onChange={(e) => setContent(e.target.value)} />

            <div style={{ display: "flex", gap: 6 }}>
              <input
                className="qinput"
                placeholder="rewrite instruction, e.g. “shorter, warmer, keep the mandated sentence”"
                value={aiInstr}
                onChange={(e) => setAiInstr(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && content && !aiBusy && rewrite()}
              />
              <button className="btn" disabled={aiBusy || !content} onClick={rewrite} title="Rewrite with the builder model — preview first, nothing saved">
                <Sparkles /> {aiBusy ? "Rewriting…" : "Rewrite with AI"}
              </button>
            </div>
            {proposal && (
              <div className="card" style={{ border: "1px solid var(--accent)" }}>
                <div className="chead">
                  <h3>AI proposal</h3>
                  <span className="sub">{proposal.notes || "review, then accept into the editor"}</span>
                </div>
                <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.55 }}>{proposal.content}</div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      className="btn primary sm"
                      onClick={() => {
                        setContent(proposal.content);
                        setProposal(null);
                        setNote("proposal accepted into the editor — review and Save draft / publish when ready");
                      }}
                    >
                      <Check /> Use this text
                    </button>
                    <button className="btn ghost sm" onClick={() => setProposal(null)}>
                      <X /> Discard
                    </button>
                  </div>
                </div>
              </div>
            )}

            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn" disabled={busy || !name || !content} onClick={() => save(false)}>
                <Save /> Save draft
              </button>
              <button className="btn primary" disabled={busy || !name || !content} onClick={() => save(true)}>
                <BadgeCheck /> Save & publish
              </button>
            </div>
            {note && <p style={{ margin: 0, color: "var(--muted)", fontSize: 12.5 }}>{note}</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
