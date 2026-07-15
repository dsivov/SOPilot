import { BadgeCheck, Save } from "lucide-react";
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
                    <tr><th>Name</th><th>Kind</th><th>Version</th><th>Status</th></tr>
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
