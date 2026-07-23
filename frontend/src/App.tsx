import { Blocks, Database, FileText, Gauge, Headphones, MessagesSquare, Moon, Network, Plug, ShieldCheck, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, apiRaw, clearCreds, getCreds, setCreds } from "./api";
import SopsView from "./views/Sops";
import BlocksView from "./views/Blocks";
import SessionsView from "./views/Sessions";
import PlaygroundView from "./views/Playground";
import DashboardView from "./views/Dashboard";
import TracesView from "./views/Traces";
import ConnectorsView from "./views/Connectors";
import ConfigView from "./views/Config";
import ConfigAdminView from "./views/ConfigAdmin";
import AdminConsole from "./views/AdminConsole";

type ViewId = "sops" | "blocks" | "config" | "configAdmin" | "dashboard" | "playground" | "sessions" | "traces" | "connectors";

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

  if (adminMode) return <AdminConsole onExit={() => setAdminMode(false)} />;
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
        { id: "configAdmin", label: "Config admin", icon: <ShieldCheck /> },
        { id: "config", label: "Config viewer", icon: <Network /> },
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
            {view === "configAdmin" && <ConfigAdminView />}
            {view === "config" && <ConfigView />}
          </div>
        </div>
      </div>
    </div>
  );
}
