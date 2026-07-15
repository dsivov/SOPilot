import { Blocks, FileText, Headphones, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, clearCreds, getCreds, setCreds } from "./api";
import SopsView from "./views/Sops";
import BlocksView from "./views/Blocks";
import SessionsView from "./views/Sessions";

type ViewId = "sops" | "blocks" | "sessions";

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

function Connect({ onDone }: { onDone: () => void }) {
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
            <input className="qinput" placeholder="project slug" value={project} onChange={(e) => setProject(e.target.value)} />
            {error && (
              <span className="chip crit" style={{ whiteSpace: "normal" }}>
                <span className="cd" />
                {error}
              </span>
            )}
            <button className="btn primary" disabled={!key || !project || checking} onClick={connect}>
              {checking ? "Checking…" : "Connect"}
            </button>
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

  useEffect(() => {
    const onAuthFail = () => setConnected(false); // stale/typo'd key → back to Connect with the error visible
    window.addEventListener("sopilot-auth-failed", onAuthFail);
    return () => window.removeEventListener("sopilot-auth-failed", onAuthFail);
  }, []);

  if (!connected) return <Connect onDone={() => setConnected(true)} />;
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
      ],
    },
    {
      grp: "Operations",
      items: [{ id: "sessions", label: "Sessions", icon: <Headphones /> }],
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
              <div style={{ fontSize: 13, fontWeight: 600 }}>{project}</div>
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
            <span className="ctx">
              <span className="dot" />
              {project}
            </span>
            <div style={{ flex: 1 }} />
            <button className="iconbtn" title="Toggle theme" onClick={toggleTheme}>
              {dark ? <Sun /> : <Moon />}
            </button>
          </header>
          <div className="content">
            {view === "sops" && <SopsView />}
            {view === "blocks" && <BlocksView />}
            {view === "sessions" && <SessionsView />}
          </div>
        </div>
      </div>
    </div>
  );
}
