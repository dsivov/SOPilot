import { CheckCircle2, FileUp, Save, Send, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, apiUpload } from "../api";
import GraphView from "./GraphView";

type SopMeta = { id: string; name: string; latest_version: number; updated_at: string };
type Lint = { problems: string[]; publishable: boolean };
type ChatMsg = { role: "user" | "assistant"; content: string };

export default function SopsView() {
  const [sops, setSops] = useState<SopMeta[]>([]);
  const [selected, setSelected] = useState<SopMeta | null>(null);
  const [status, setStatus] = useState<string>("");
  const [text, setText] = useState("");
  const [lint, setLint] = useState<Lint | null>(null);
  const [busy, setBusy] = useState("");
  const [ingestOpen, setIngestOpen] = useState(false);
  const [doc, setDoc] = useState("");
  const [docName, setDocName] = useState("");
  const [docFile, setDocFile] = useState<File | null>(null);
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [tab, setTab] = useState<"graph" | "json">("graph");
  const lintTimer = useRef<number | undefined>(undefined);

  const parsedDef = useMemo(() => {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }, [text]);

  const refresh = useCallback(async () => {
    setSops(await api<SopMeta[]>("GET", "/sops"));
  }, []);
  useEffect(() => {
    refresh().catch((e) => setStatus(String(e)));
  }, [refresh]);

  const openSop = async (meta: SopMeta) => {
    const full = await api("GET", `/sops/${meta.id}`);
    setSelected(meta);
    setStatus(full.status);
    setChat([]);
    setText(JSON.stringify(full.definition, null, 2));
    runLint(JSON.stringify(full.definition));
  };

  const runLint = (raw: string) => {
    window.clearTimeout(lintTimer.current);
    lintTimer.current = window.setTimeout(async () => {
      try {
        const definition = JSON.parse(raw);
        setLint(await api<Lint>("POST", "/sops/lint-definition", { definition }));
      } catch (e) {
        setLint({ problems: [`json: ${e}`], publishable: false });
      }
    }, 450);
  };

  const onEdit = (v: string) => {
    setText(v);
    runLint(v);
  };

  const save = async () => {
    if (!selected) return;
    setBusy("save");
    try {
      const definition = JSON.parse(text);
      const meta = await api<SopMeta>("PUT", `/sops/${selected.id}`, { definition });
      setSelected(meta);
      setStatus("draft");
      await refresh();
    } catch (e) {
      setLint({ problems: [String(e instanceof ApiError ? e.message : e)], publishable: false });
    } finally {
      setBusy("");
    }
  };

  const publish = async () => {
    if (!selected) return;
    setBusy("publish");
    try {
      await save();
      const r = await api("POST", `/sops/${selected.id}/publish`);
      setStatus(r.status);
    } catch (e) {
      if (e instanceof ApiError && typeof e.detail === "object" && e.detail && "problems" in (e.detail as any)) {
        setLint({ problems: (e.detail as any).problems, publishable: false });
      } else setLint({ problems: [String(e)], publishable: false });
    } finally {
      setBusy("");
    }
  };

  const ingest = async () => {
    setBusy("ingest");
    try {
      const r = docFile
        ? await apiUpload("/sops/ingest-file", docFile, { name_hint: docName })
        : await api("POST", "/sops/ingest", { text: doc, name_hint: docName });
      setIngestOpen(false);
      setDoc("");
      setDocFile(null);
      await refresh();
      setSelected({ id: r.id, name: r.name, latest_version: r.version, updated_at: "" });
      setStatus("draft");
      setText(JSON.stringify(r.definition, null, 2));
      setLint(r.lint);
      setChat([{ role: "assistant", content: "Draft created from your document. Tell me what to adjust — stages, wording, triggers, data lookups." }]);
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy("");
    }
  };

  const sendChat = async () => {
    if (!chatInput.trim() || !text) return;
    const history = [...chat, { role: "user" as const, content: chatInput }];
    setChat(history);
    setChatInput("");
    setBusy("chat");
    try {
      const definition = JSON.parse(text);
      const r = await api("POST", "/sops/build-turn", { history, current_definition: definition });
      setChat([...history, { role: "assistant", content: r.assistant_message }]);
      setText(JSON.stringify(r.definition, null, 2));
      setLint(r.lint);
    } catch (e) {
      setChat([...history, { role: "assistant", content: `That change failed: ${e}` }]);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="view">
      <div className="phead">
        <div>
          <div className="eyebrow">Studio</div>
          <h1>Standard Operating Procedures</h1>
          <p>Draft from a document, refine in conversation, publish behind the lint gate.</p>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => setIngestOpen(true)}>
            <FileUp /> New from document
          </button>
          <button className="btn" disabled={!selected || busy !== ""} onClick={save}>
            <Save /> Save draft
          </button>
          <button className="btn primary" disabled={!selected || busy !== "" || !lint?.publishable} onClick={publish}>
            <ShieldCheck /> Publish
          </button>
        </div>
      </div>

      <div className="grid2">
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <div className="card">
            <div className="chead">
              <h3>Library</h3>
              <span className="sub num">{sops.length} SOPs</span>
            </div>
            <div className="cbody" style={{ padding: 0 }}>
              {sops.length === 0 ? (
                <div className="empty">No SOPs yet — start from a document.</div>
              ) : (
                <div className="tablewrap" style={{ border: 0, borderRadius: 0, maxHeight: 180 }}>
                  <table className="table">
                    <thead>
                      <tr><th>Name</th><th>Version</th><th>Updated</th></tr>
                    </thead>
                    <tbody>
                      {sops.map((s) => (
                        <tr key={s.id} className={selected?.id === s.id ? "sel" : ""} onClick={() => openSop(s)} style={{ cursor: "pointer" }}>
                          <td style={{ fontWeight: 600 }}>{s.name}</td>
                          <td className="mono num">v{s.latest_version}</td>
                          <td style={{ color: "var(--muted)", fontSize: 12, whiteSpace: "nowrap" }}>{s.updated_at.slice(0, 16).replace("T", " ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          <div className="card" style={{ flex: 1 }}>
            <div className="chead">
              <h3>Definition{selected ? ` — ${selected.name}` : ""}</h3>
              {selected && (
                <span className={"chip " + (status === "published" ? "good" : "warn")}>
                  <span className="cd" />
                  {status || "draft"}
                </span>
              )}
              <span className="sub" style={{ display: "flex", gap: 6 }}>
                <button className={"btn sm" + (tab === "graph" ? " primary" : " ghost")} onClick={() => setTab("graph")}>
                  Graph
                </button>
                <button className={"btn sm" + (tab === "json" ? " primary" : " ghost")} onClick={() => setTab("json")}>
                  JSON
                </button>
              </span>
            </div>
            <div className="cbody">
              {!selected ? (
                <div className="empty">Select an SOP or create one from a document.</div>
              ) : tab === "json" ? (
                <textarea className="area mono" rows={22} value={text} onChange={(e) => onEdit(e.target.value)} spellCheck={false} />
              ) : parsedDef ? (
                <GraphView def={parsedDef} />
              ) : (
                <div className="empty">JSON is currently invalid — fix it in the JSON tab to see the graph.</div>
              )}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <div className="card">
            <span className={"stripe " + (lint ? (lint.publishable ? "good" : "crit") : "accent")} />
            <div className="chead">
              <h3>Lint</h3>
              {lint && (
                <span className={"chip " + (lint.publishable ? "good" : "crit")}>
                  <span className="cd" />
                  {lint.publishable ? "publishable" : `${lint.problems.length} problem${lint.problems.length === 1 ? "" : "s"}`}
                </span>
              )}
            </div>
            <div className="cbody">
              {!lint ? (
                <div className="empty">Lint results appear as you edit.</div>
              ) : lint.publishable ? (
                <div className="lintline">
                  <CheckCircle2 size={15} style={{ color: "var(--good)", flex: "0 0 15px", marginTop: 2 }} />
                  Structure is clean: no cycles, no unreachable stages, all references resolve.
                </div>
              ) : (
                lint.problems.map((p, i) => (
                  <div className="lintline" key={i}>
                    <span className="st crit" style={{ marginTop: 2 }} />
                    {p}
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="card" style={{ flex: 1 }}>
            <div className="chead">
              <h3>Refine in conversation</h3>
              <span className="sub">smallest change per turn</span>
            </div>
            <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="chatlog">
                {chat.length === 0 && <div className="empty">e.g. “Identity must be verified before any account details” or “add a PriceShopper cohort”.</div>}
                {chat.map((m, i) => (
                  <div key={i} className={"msg " + m.role}>{m.content}</div>
                ))}
                {busy === "chat" && <div className="spin" />}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  className="qinput"
                  placeholder={selected ? "Describe a change…" : "Select an SOP first"}
                  disabled={!selected || busy === "chat"}
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && sendChat()}
                />
                <button className="btn primary" disabled={!selected || busy === "chat" || !chatInput.trim()} onClick={sendChat}>
                  <Send />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {ingestOpen && (
        <div className="modal-overlay" onClick={() => busy !== "ingest" && setIngestOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="mhead">
              <FileUp size={18} style={{ color: "var(--accent)", marginTop: 2 }} />
              <div>
                <h3>New SOP from a document</h3>
                <p style={{ margin: 0, color: "var(--muted)", fontSize: 12.5 }}>
                  Paste the written procedure (policy, call script). You get a draft conversation graph to refine — nothing goes live without the lint gate and publish.
                </p>
              </div>
            </div>
            <div className="mbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <input className="qinput" placeholder="SOP name (optional)" value={docName} onChange={(e) => setDocName(e.target.value)} />
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <label className="btn sm" style={{ margin: 0 }}>
                  <FileUp /> Upload PDF / text
                  <input
                    type="file"
                    accept=".pdf,.txt,.md"
                    style={{ display: "none" }}
                    onChange={(e) => {
                      const f = e.target.files?.[0] ?? null;
                      setDocFile(f);
                      if (f && !docName) setDocName(f.name.replace(/\.(pdf|txt|md)$/i, "").replace(/[_-]+/g, " "));
                    }}
                  />
                </label>
                {docFile ? (
                  <span className="chip accent"><span className="cd" />{docFile.name}</span>
                ) : (
                  <span style={{ color: "var(--muted)", fontSize: 12.5 }}>or paste text below</span>
                )}
              </div>
              <textarea
                className="area"
                rows={12}
                placeholder="Paste the procedure text here…"
                value={doc}
                disabled={!!docFile}
                onChange={(e) => setDoc(e.target.value)}
              />
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <button className="btn ghost" disabled={busy === "ingest"} onClick={() => setIngestOpen(false)}>Cancel</button>
                <button className="btn primary" disabled={(!doc.trim() && !docFile) || busy === "ingest"} onClick={ingest}>
                  {busy === "ingest" ? "Drafting…" : "Create draft"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
