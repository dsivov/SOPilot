import { Blocks, Database, Download, FileText, Gauge, Headphones, MessagesSquare, Moon, Plug, Sun, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, apiRaw, clearCreds, getCreds, setCreds } from "./api";
import SopsView from "./views/Sops";
import BlocksView from "./views/Blocks";
import SessionsView from "./views/Sessions";
import PlaygroundView from "./views/Playground";
import DashboardView from "./views/Dashboard";
import TracesView from "./views/Traces";
import ConnectorsView from "./views/Connectors";
import AdminConsole from "./views/AdminConsole";

type ViewId = "sops" | "blocks" | "dashboard" | "playground" | "sessions" | "traces" | "connectors";

function BrandMark() {
  // four token-colored dots joined by line2 edges (per the design guide §11)
  return (
    <svg className="mark" viewBox="0 0 26 26" aria-hidden>
      <path d="M6 7 L20 7 M6 7 L6 19 M20 7 L13 19 M6 19 L13 19" stroke="var(--line2)" strokeWidth="1.5" fill="none" />
      <circle cx="6" cy="7" r="3.4" fill="var(--accent)" />
      <circle cx="20" cy="7" r="3.4" fill="var(--comm)" />
      <circle cx="6" cy="19" r="3.4" fill="var(--good)" />
      <circle cx="13" cy="19" r="3.4" fill="var(--warn)" />
    </svg>
  );
}

function ProjectPicker({ apiKey, onPick }: { apiKey: string; onPick: (slug: string) => void }) {
  const [rows, setRows] = useState<Array<{ slug: string; name: string; subsystems: string; sops: string[] }> | null>(null);
  const [tenant, setTenant] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        apiRaw<{ tenant_slug: string }>("GET", "/admin/whoami", { key: apiKey })
          .then((w) => alive && setTenant(w.tenant_slug))
          .catch(() => undefined);
        const projects = await apiRaw<Array<{ slug: string; name: string; subsystems: string }>>(
          "GET", "/admin/projects", { key: apiKey },
        );
        const withSops = await Promise.all(
          projects.map(async (p) => {
            try {
              const sops = await apiRaw<Array<{ name: string }>>("GET", "/sops", { key: apiKey, project: p.slug });
              return { ...p, sops: sops.map((x) => x.name) };
            } catch {
              return { ...p, sops: [] };
            }
          }),
        );
        if (alive) setRows(withSops);
      } catch (e) {
        if (alive) setErr(e instanceof ApiError && e.status === 401 ? "API key rejected (401)." : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [apiKey]);

  if (err) return <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{err}</span>;
  if (!rows) return <div className="spin" />;
  if (rows.length === 0) return <div className="empty">This tenant has no projects yet.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {tenant && (
        <span style={{ fontSize: 11.5, color: "var(--muted)" }}>
          This API key belongs to tenant <b style={{ color: "var(--text2)" }}>{tenant}</b> — it can only see the
          projects below. Other tenants need their own key.
        </span>
      )}
      {rows.map((p) => (
        <button
          key={p.slug}
          className="btn"
          style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 4, padding: "10px 13px" }}
          onClick={() => onPick(p.slug)}
        >
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontWeight: 700 }}>{p.name || p.slug}</span>
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>{p.slug}</span>
            <span className={"chip " + (p.subsystems === "default" || p.subsystems === "both" || !p.subsystems ? "accent" : "comm")}>
              {p.subsystems || "default"}
            </span>
          </span>
          <span style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
            {p.sops.length === 0 ? (
              <span style={{ fontSize: 11.5, color: "var(--muted)", fontWeight: 400 }}>no SOPs yet</span>
            ) : (
              p.sops.slice(0, 4).map((n) => <span key={n} className="chip">{n}</span>)
            )}
            {p.sops.length > 4 && <span className="chip">+{p.sops.length - 4} more</span>}
          </span>
        </button>
      ))}
    </div>
  );
}

// Topbar project tools: subsystem mode (SOP / background retrieval / both /
// advisory) and full-config export/import (SOPs + prompt blocks + connectors).
const SUBSYSTEM_MODES = [
  { value: "both", label: "SOP + retrieval" },
  { value: "sop", label: "SOP only" },
  { value: "retrieval", label: "Retrieval only" },
  { value: "advisory", label: "Advisory" },
] as const;

function ProjectTools({ project }: { project: string }) {
  const [mode, setMode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<{ summary: any; warnings: string[] } | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api<Array<{ slug: string; subsystems: string }>>("GET", "/admin/projects")
      .then((ps) => {
        const p = ps.find((x) => x.slug === project);
        setMode(!p || p.subsystems === "default" || !p.subsystems ? "both" : p.subsystems);
      })
      .catch(() => setMode("both"));
  }, [project]);

  const setSubsystems = async (v: string) => {
    const prev = mode;
    setMode(v);
    try { await api("PATCH", `/admin/projects/${project}`, { subsystems: v }); }
    catch (e: any) { setMode(prev); setErr(`Mode change failed: ${e?.message ?? e}`); }
  };

  const exportConfig = async () => {
    setBusy(true); setErr("");
    try {
      const bundle = await api<any>("GET", "/project/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: `sopilot-${project}-export.json` });
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setErr(`Export failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  const importConfig = async (file: File) => {
    setBusy(true); setErr("");
    try {
      const bundle = JSON.parse(await file.text());
      const r = await api<{ summary: any; warnings: string[] }>("POST", "/project/import", bundle);
      setReport(r);
      window.dispatchEvent(new Event("sopilot-project-imported")); // views can refetch
    } catch (e: any) { setErr(`Import failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  return (
    <>
      <select
        className="qinput" title="Which subsystems run for this project's sessions"
        style={{ width: "auto", padding: "5px 8px", fontSize: 12.5 }}
        value={mode ?? "both"} disabled={mode === null}
        onChange={(e) => setSubsystems(e.target.value)}
      >
        {SUBSYSTEM_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
      </select>
      <button className="btn ghost sm" onClick={exportConfig} disabled={busy} title="Download this project's full config (SOPs, prompt blocks, connectors) as JSON">
        <Download size={14} style={{ marginRight: 4, verticalAlign: "-2px" }} />Export
      </button>
      <label className="btn ghost sm" title="Import a project-config JSON — items are matched by name (new version for existing, created otherwise)" style={{ cursor: "pointer" }}>
        <Upload size={14} style={{ marginRight: 4, verticalAlign: "-2px" }} />Import
        <input type="file" accept="application/json,.json" style={{ display: "none" }}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) importConfig(f); e.target.value = ""; }} />
      </label>
      {err && <span className="chip crit" style={{ maxWidth: 340, whiteSpace: "normal" }}><span className="cd" />{err}</span>}
      {report && (
        <div className="modal-overlay" onClick={() => setReport(null)}>
          <div className="modal" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
            <div className="mhead"><h3>Import complete</h3></div>
            <div className="mbody" style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13 }}>
              {(["sops", "prompt_blocks", "connectors"] as const).map((k) => (
                <div key={k} style={{ display: "flex", gap: 10 }}>
                  <b style={{ flex: "0 0 120px", textTransform: "capitalize" }}>{k.replace("_", " ")}</b>
                  <span className="sub">
                    {report.summary[k].created} created · {report.summary[k].updated} updated · {report.summary[k].published} published
                  </span>
                </div>
              ))}
              {report.warnings.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  {report.warnings.map((w, i) => (
                    <div key={i} className="lintline" style={{ color: "var(--warn)", fontSize: 12.5 }}>⚠ {w}</div>
                  ))}
                </div>
              )}
              <button className="btn primary sm" style={{ alignSelf: "flex-end", marginTop: 6 }}
                onClick={() => { setReport(null); window.location.assign(window.location.pathname); }}>
                Reload Studio
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function Connect({ onDone, onAdmin }: { onDone: () => void; onAdmin: () => void }) {
  const [key, setKey] = useState(getCreds().key);
  const [project, setProject] = useState(getCreds().project);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  const connect = async () => {
    setChecking(true);
    setError("");
    setCreds(key.trim(), project.trim());
    try {
      await api("GET", "/sops"); // validates key + project before entering
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setError("API key rejected (401) — check for typos or missing characters.");
      else if (e instanceof ApiError && e.status === 404) setError(`Project “${project.trim()}” not found in this tenant (404).`);
      else setError(`Cannot reach the API: ${e}`);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="sopilot">
      <div className="connect">
        <div className="card" style={{ width: 440 }}>
          {error && <span className="stripe crit" />}
          <div className="chead" style={{ paddingBottom: 0 }}>
            <BrandMark />
            <h3>Connect to SOPilot</h3>
          </div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <p style={{ margin: 0, color: "var(--muted)", fontSize: 13 }}>
              Paste a tenant API key and the project slug. Stored locally in this browser only.
            </p>
            <input className="qinput mono" placeholder="sop_…" value={key} onChange={(e) => setKey(e.target.value)} />
            {key.trim().length > 10 && (
              <>
                <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)" }}>
                  Projects in this tenant — click to connect
                </div>
                <ProjectPicker
                  apiKey={key.trim()}
                  onPick={(slug) => {
                    setProject(slug);
                    setCreds(key.trim(), slug);
                    onDone();
                  }}
                />
              </>
            )}
            <input className="qinput" placeholder="or type a project slug" value={project} onChange={(e) => setProject(e.target.value)} />
            {error && (
              <span className="chip crit" style={{ whiteSpace: "normal" }}>
                <span className="cd" />
                {error}
              </span>
            )}
            <button className="btn primary" disabled={!key || !project || checking} onClick={connect}>
              {checking ? "Checking…" : "Connect"}
            </button>
            <button className="btn ghost sm" style={{ alignSelf: "center" }} onClick={onAdmin}>Platform admin →</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<ViewId>("sops");
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"));
  const [connected, setConnected] = useState(Boolean(getCreds().key && getCreds().project));
  const [adminMode, setAdminMode] = useState(false);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [tenantSlug, setTenantSlug] = useState("");

  useEffect(() => {
    if (!connected) return;
    api<{ tenant_slug: string }>("GET", "/admin/whoami")
      .then((w) => setTenantSlug(w.tenant_slug))
      .catch(() => setTenantSlug(""));
  }, [connected]);

  useEffect(() => {
    const onAuthFail = () => setConnected(false); // stale/typo'd key → back to Connect with the error visible
    window.addEventListener("sopilot-auth-failed", onAuthFail);
    return () => window.removeEventListener("sopilot-auth-failed", onAuthFail);
  }, []);

  // One-click tenant login from the admin console: creds are set with the minted
  // (hidden) key; with a single project we land in the Studio directly, otherwise
  // the Connect screen opens on its project picker.
  const adminLogin = (key: string, project: string) => {
    setCreds(key, project);
    setAdminMode(false);
    setConnected(Boolean(project));
  };

  if (adminMode) return <AdminConsole onExit={() => setAdminMode(false)} onLogin={adminLogin} />;
  if (!connected) return <Connect onDone={() => setConnected(true)} onAdmin={() => setAdminMode(true)} />;
  const { project } = getCreds();

  const toggleTheme = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("sopilot-theme", next ? "dark" : "light");
    setDark(next);
  };

  const nav: Array<{ grp: string; items: Array<{ id: ViewId; label: string; icon: JSX.Element }> }> = [
    {
      grp: "Studio",
      items: [
        { id: "sops", label: "SOPs", icon: <FileText /> },
        { id: "blocks", label: "Prompt blocks", icon: <Blocks /> },
        { id: "connectors", label: "Connectors", icon: <Plug /> },
      ],
    },
    {
      grp: "Operations",
      items: [
        { id: "dashboard", label: "Dashboard", icon: <Gauge /> },
        { id: "playground", label: "Playground", icon: <MessagesSquare /> },
        { id: "sessions", label: "Sessions", icon: <Headphones /> },
        { id: "traces", label: "Traces", icon: <Database /> },
      ],
    },
  ];

  return (
    <div className="sopilot">
      <div className="app">
        <aside className="side">
          <div className="brand">
            <BrandMark />
            <div className="name">
              SOPilot
              <small>conversation studio</small>
            </div>
          </div>
          <nav className="nav">
            {nav.map((g) => (
              <div key={g.grp}>
                <div className="grp">{g.grp}</div>
                {g.items.map((it) => (
                  <button
                    key={it.id}
                    className={"navitem" + (view === it.id ? " active" : "")}
                    onClick={() => setView(it.id)}
                  >
                    {it.icon}
                    {it.label}
                  </button>
                ))}
              </div>
            ))}
          </nav>
          <div className="foot">
            <div className="avatar">{project.slice(0, 2).toUpperCase()}</div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{tenantSlug ? `${tenantSlug} · ${project}` : project}</div>
              <button
                className="btn ghost sm"
                style={{ padding: 0, fontSize: 11.5 }}
                onClick={() => {
                  clearCreds();
                  setConnected(false);
                }}
              >
                disconnect
              </button>
            </div>
          </div>
        </aside>
        <div className="main">
          <header className="topbar">
            <button className="ctx" title="Switch project" style={{ cursor: "pointer" }} onClick={() => setSwitcherOpen(true)}>
              <span className="dot" />
              {tenantSlug ? `${tenantSlug} · ${project}` : project} ▾
            </button>
            <div style={{ flex: 1 }} />
            <ProjectTools project={project} />
            <button className="iconbtn" title="Toggle theme" onClick={toggleTheme}>
              {dark ? <Sun /> : <Moon />}
            </button>
          </header>
          {switcherOpen && (
            <div className="modal-overlay" onClick={() => setSwitcherOpen(false)}>
              <div className="modal" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
                <div className="mhead">
                  <h3>Switch project</h3>
                </div>
                <div className="mbody">
                  <ProjectPicker
                    apiKey={getCreds().key}
                    onPick={(slug) => {
                      setCreds(getCreds().key, slug);
                      window.location.assign(window.location.pathname); // clean state for the new scope
                    }}
                  />
                </div>
              </div>
            </div>
          )}
          <div className="content">
            {view === "sops" && <SopsView />}
            {view === "blocks" && <BlocksView />}
            {view === "dashboard" && <DashboardView />}
            {view === "playground" && <PlaygroundView />}
            {view === "sessions" && <SessionsView />}
            {view === "traces" && <TracesView />}
            {view === "connectors" && <ConnectorsView />}
          </div>
        </div>
      </div>
    </div>
  );
}
