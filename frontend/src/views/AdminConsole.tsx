// Platform admin console — RBAC management for the whole SOPilot instance.
//
// Authenticated with the deployment admin token (X-Admin-Token), NOT a tenant key.
// Lets the operator add/delete tenants and mint / revoke each tenant's sop_ keys.
// A minted key is shown exactly once (only its sha256 is stored) — copy it to the
// tenant owner right then.
import { useEffect, useState } from "react";
import { ApiError } from "../api";
import { adminApi, clearAdminToken, getAdminToken, setAdminToken, type AdminKey, type AdminTenant } from "../adminApi";

function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked */ }
  };
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <code className="mono" style={{ flex: 1, padding: "8px 10px", background: "var(--panel2, rgba(127,127,127,.12))", borderRadius: 6, wordBreak: "break-all", fontSize: 13 }}>{value}</code>
      <button className="btn sm" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
    </div>
  );
}

// The one-time reveal of a freshly minted raw key.
function Reveal({ title, apiKey, onDismiss }: { title: string; apiKey: string; onDismiss: () => void }) {
  return (
    <div className="card" style={{ marginBottom: 16, borderColor: "var(--good)" }}>
      <span className="stripe good" />
      <div className="chead"><span>{title}</span>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={onDismiss}>Dismiss</button></div>
      <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div className="sub" style={{ color: "var(--warn)" }}>⚠ Shown once — copy it to the tenant owner now. Only its hash is stored; it can't be retrieved again.</div>
        <CopyField value={apiKey} />
      </div>
    </div>
  );
}

function KeysPanel({ slug, onReveal, onKeysChanged }: { slug: string; onReveal: (title: string, key: string) => void; onKeysChanged: () => void }) {
  const [keys, setKeys] = useState<AdminKey[] | null>(null);
  const [err, setErr] = useState("");
  const [label, setLabel] = useState("");
  const [role, setRole] = useState("runtime");
  const [busy, setBusy] = useState(false);

  const load = () => adminApi<AdminKey[]>("GET", `/admin/tenants/${slug}/keys`).then(setKeys).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [slug]);

  const issue = async () => {
    setBusy(true); setErr("");
    try {
      const r = await adminApi<{ api_key: string; label: string }>("POST", `/admin/tenants/${slug}/keys`, { label, role });
      onReveal(`New ${role} key for ${slug}${r.label ? ` · ${r.label}` : ""}`, r.api_key);
      setLabel(""); await load(); onKeysChanged();
    } catch (e: any) { setErr(String(e?.message ?? e)); } finally { setBusy(false); }
  };
  const revoke = async (id: string) => {
    setErr("");
    try { await adminApi("POST", `/admin/tenants/${slug}/keys/${id}/revoke`); await load(); onKeysChanged(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
  };

  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--line)" }}>
      {err && <div className="lintline" style={{ color: "var(--crit)", marginBottom: 6 }}>{err}</div>}
      {keys === null ? <div className="sub">Loading keys…</div> : keys.length === 0 ? <div className="empty" style={{ padding: "6px 0" }}>No keys yet.</div> : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 }}>
          {keys.map((k) => (
            <div key={k.id} style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 12.5, padding: "3px 0" }}>
              <span className={"chip " + (k.role === "admin" ? "good" : "muted")} style={{ flex: "0 0 auto" }}><span className="cd" />{k.role}</span>
              <span style={{ flex: 1, minWidth: 0, textDecoration: k.revoked ? "line-through" : "none", color: k.revoked ? "var(--muted)" : "var(--text)" }}>
                {k.label} <span className="mono sub">· sop_…{k.hash_prefix}</span>
              </span>
              {k.revoked
                ? <span className="sub">revoked</span>
                : <button className="btn ghost sm" onClick={() => revoke(k.id)}>Revoke</button>}
            </div>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input className="qinput" style={{ flex: 1, minWidth: 140 }} placeholder="key label (e.g. prod, ci)" value={label} onChange={(e) => setLabel(e.target.value)} />
        <select className="qinput" style={{ width: "auto" }} value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="runtime">runtime</option><option value="admin">admin</option>
        </select>
        <button className="btn sm primary" onClick={issue} disabled={busy}>{busy ? "Issuing…" : "Issue key"}</button>
      </div>
    </div>
  );
}

// Per-project full-config export/import (SOPs + prompt blocks + connectors),
// admin-token side — same bundle format as the Studio topbar's Export/Import.
function ProjectsPanel({ slug }: { slug: string }) {
  const [projects, setProjects] = useState<Array<{ slug: string; name: string; subsystems: string }> | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [newSlug, setNewSlug] = useState("");

  const load = () =>
    adminApi<Array<{ slug: string; name: string; subsystems: string }>>("GET", `/admin/tenants/${slug}/projects`)
      .then(setProjects).catch((e) => setMsg(String(e?.message ?? e)));
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [slug]);

  // A fresh tenant has no projects — without one there is nothing to log into.
  const createProject = async () => {
    if (!newSlug.trim()) return;
    setBusy(true); setMsg("");
    try {
      await adminApi("POST", `/admin/tenants/${slug}/projects`, { slug: newSlug.trim() });
      setNewSlug(""); await load();
    } catch (e: any) { setMsg(`Create failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  const exportProject = async (p: string) => {
    setBusy(true); setMsg("");
    try {
      const bundle = await adminApi<any>("GET", `/admin/tenants/${slug}/projects/${p}/export`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: `sopilot-${slug}-${p}-export.json` });
      a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setMsg(`Export failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  const importProject = async (p: string, file: File) => {
    setBusy(true); setMsg("");
    try {
      const bundle = JSON.parse(await file.text());
      const r = await adminApi<{ summary: any; warnings: string[] }>("POST", `/admin/tenants/${slug}/projects/${p}/import`, bundle);
      const s = r.summary;
      setMsg(`Imported into ${p}: ` +
        (["sops", "prompt_blocks", "connectors"] as const)
          .map((k) => `${k.replace("_", " ")} +${s[k].created}/${s[k].updated}↑/${s[k].published}★`).join(" · ") +
        (r.warnings.length ? ` — ${r.warnings.length} warning(s): ${r.warnings[0]}` : ""));
    } catch (e: any) { setMsg(`Import failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--line)" }}>
      <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".5px", textTransform: "uppercase", color: "var(--muted)", marginBottom: 6 }}>
        Projects — full-config export / import
      </div>
      {msg && <div className="lintline" style={{ color: msg.startsWith("Imported") ? "var(--good)" : "var(--crit)", marginBottom: 6, whiteSpace: "normal" }}>{msg}</div>}
      {projects === null ? <div className="sub">Loading projects…</div> : projects.length === 0 ? <div className="empty" style={{ padding: "6px 0" }}>No projects yet — create one below (a tenant needs a project before anyone can log into its Studio).</div> : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {projects.map((p) => (
            <div key={p.slug} style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 12.5, padding: "3px 0" }}>
              <span style={{ flex: 1, minWidth: 0 }}>{p.name || p.slug} <span className="mono sub">· {p.slug}</span></span>
              <span className="chip muted"><span className="cd" />{p.subsystems}</span>
              <button className="btn ghost sm" disabled={busy} onClick={() => exportProject(p.slug)}>Export</button>
              <label className="btn ghost sm" style={{ cursor: "pointer" }}>Import
                <input type="file" accept="application/json,.json" style={{ display: "none" }}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) importProject(p.slug, f); e.target.value = ""; }} />
              </label>
            </div>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
        <input className="qinput mono" style={{ flex: 1, minWidth: 120 }} placeholder="new project slug (e.g. main)"
          value={newSlug} onChange={(e) => setNewSlug(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && createProject()} />
        <button className="btn sm" onClick={createProject} disabled={busy || !newSlug.trim()}>Create project</button>
      </div>
    </div>
  );
}

function Console({ onExit, onLogin }: { onExit: () => void; onLogin: (key: string, project: string) => void }) {
  const [tenants, setTenants] = useState<AdminTenant[] | null>(null);
  const [err, setErr] = useState("");
  const [reveal, setReveal] = useState<{ title: string; apiKey: string } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [firstProject, setFirstProject] = useState("main");
  const [busy, setBusy] = useState(false);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState<string | null>(null);
  // Bundle import at console level: target tenant/project are free slugs —
  // missing ones are created server-side (fresh-deployment restore path).
  const [pendingImport, setPendingImport] = useState<{ bundle: any; tenant: string; project: string } | null>(null);
  const [ioMsg, setIoMsg] = useState("");

  const pickBundle = async (file: File) => {
    setIoMsg("");
    try {
      const bundle = JSON.parse(await file.text());
      if (bundle?.kind !== "sopilot-project-export") { setIoMsg("Not a SOPilot project-export bundle."); return; }
      setPendingImport({ bundle, tenant: "", project: String(bundle.project?.slug || "main") });
    } catch (e: any) { setIoMsg(`Cannot read bundle: ${e?.message ?? e}`); }
  };
  const runImport = async () => {
    if (!pendingImport) return;
    const { bundle, tenant, project } = pendingImport;
    setBusy(true); setIoMsg("");
    try {
      const r = await adminApi<{ summary: any; warnings: string[] }>(
        "POST", `/admin/tenants/${tenant.trim()}/projects/${project.trim()}/import`, bundle);
      const s = r.summary;
      setIoMsg(`Imported into ${tenant}/${project}: ` +
        (["sops", "prompt_blocks", "connectors"] as const)
          .map((k) => `${k.replace("_", " ")} +${s[k].created}/${s[k].updated}↑/${s[k].published}★`).join(" · ") +
        (r.warnings.length ? ` — ${r.warnings.length} warning(s): ${r.warnings[0]}` : ""));
      setPendingImport(null); await load();
    } catch (e: any) { setIoMsg(`Import failed: ${e?.message ?? e}`); } finally { setBusy(false); }
  };

  // One-click login: mint a console-login key server-side (never shown) and
  // enter the Studio as this tenant — straight in if it has exactly one project.
  const login = async (s: string) => {
    setLoginBusy(s); setErr("");
    try {
      const r = await adminApi<{ api_key: string; projects: string[] }>("POST", `/admin/tenants/${s}/login-key`);
      onLogin(r.api_key, r.projects.length === 1 ? r.projects[0] : "");
    } catch (e: any) { setErr(String(e?.message ?? e)); } finally { setLoginBusy(null); }
  };

  const load = () => adminApi<AdminTenant[]>("GET", "/admin/tenants").then(setTenants).catch((e) => setErr(String(e?.message ?? e)));
  useEffect(() => { load(); }, []);

  const create = async () => {
    if (!slug.trim()) return;
    setBusy(true); setErr("");
    try {
      const r = await adminApi<{ slug: string; api_key: string; project_slug?: string }>(
        "POST", "/admin/tenants", { slug: slug.trim(), name: name.trim(), project_slug: firstProject.trim() });
      setReveal({
        title: `Tenant "${r.slug}" created` + (r.project_slug ? ` with project "${r.project_slug}"` : "") + ` · bootstrap admin key`,
        apiKey: r.api_key,
      });
      setSlug(""); setName(""); setFirstProject("main"); await load();
    } catch (e: any) { setErr(String(e?.message ?? e)); } finally { setBusy(false); }
  };
  const del = async (s: string) => {
    setErr("");
    try { await adminApi("DELETE", `/admin/tenants/${s}`); setConfirmDel(null); if (expanded === s) setExpanded(null); await load(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
  };

  return (
    <div className="sopilot" style={{ overflowY: "auto" }}>{/* .sopilot clips at 100vh — the console must scroll */}
      <div className="main" style={{ maxWidth: 780, margin: "0 auto", padding: "28px 20px", width: "100%" }}>
        <header className="topbar" style={{ marginBottom: 18 }}>
          <div className="eyebrow" style={{ margin: 0 }}>Platform admin · RBAC</div>
          <div style={{ flex: 1 }} />
          <button className="btn ghost sm" onClick={() => { clearAdminToken(); onExit(); }}>Sign out</button>
          <button className="btn ghost sm" onClick={onExit}>Tenant login →</button>
        </header>

        {err && <div className="card" style={{ marginBottom: 14, borderColor: "var(--crit)" }}><span className="stripe crit" /><div className="cbody" style={{ color: "var(--crit)" }}>{err}</div></div>}
        {reveal && <Reveal title={reveal.title} apiKey={reveal.apiKey} onDismiss={() => setReveal(null)} />}

        <div className="card" style={{ marginBottom: 16 }}>
          <div className="chead"><span>Create tenant</span>
            <label className="btn ghost sm" style={{ marginLeft: "auto", cursor: "pointer" }}
              title="Import a project-config bundle — tenant and project are created if they don't exist">
              Import bundle…
              <input type="file" accept="application/json,.json" style={{ display: "none" }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) pickBundle(f); e.target.value = ""; }} />
            </label></div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <input className="qinput mono" style={{ flex: 1, minWidth: 140 }} placeholder="slug (e.g. acme)" value={slug} onChange={(e) => setSlug(e.target.value)} />
              <input className="qinput" style={{ flex: 1, minWidth: 140 }} placeholder="display name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
              <input className="qinput mono" style={{ flex: "0 0 130px" }} placeholder="first project" title="First project, created with the tenant — clear to skip"
                value={firstProject} onChange={(e) => setFirstProject(e.target.value)} />
              <button className="btn primary" onClick={create} disabled={busy || !slug.trim()}>{busy ? "Creating…" : "Create"}</button>
            </div>
            {ioMsg && <div className="lintline" style={{ color: ioMsg.startsWith("Imported") ? "var(--good)" : "var(--crit)", whiteSpace: "normal" }}>{ioMsg}</div>}
            {pendingImport && (
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingTop: 8, borderTop: "1px solid var(--line)" }}>
                <span className="sub" style={{ flex: "0 0 auto" }}>Import bundle into</span>
                <input className="qinput mono" style={{ flex: 1, minWidth: 120 }} placeholder="tenant slug"
                  value={pendingImport.tenant} onChange={(e) => setPendingImport({ ...pendingImport, tenant: e.target.value })} />
                <span className="sub">/</span>
                <input className="qinput mono" style={{ flex: 1, minWidth: 120 }} placeholder="project slug"
                  value={pendingImport.project} onChange={(e) => setPendingImport({ ...pendingImport, project: e.target.value })} />
                <button className="btn sm primary" onClick={runImport} disabled={busy || !pendingImport.tenant.trim() || !pendingImport.project.trim()}>
                  {busy ? "Importing…" : "Import"}
                </button>
                <button className="btn ghost sm" onClick={() => setPendingImport(null)}>Cancel</button>
                <span className="sub" style={{ width: "100%" }}>Missing tenant/project are created automatically.</span>
              </div>
            )}
          </div>
        </div>

        <div className="card">
          <div className="chead"><span>Tenants{tenants ? ` (${tenants.length})` : ""}</span>
            <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={load}>Refresh</button></div>
          <div className="cbody">
            {tenants === null ? <div className="spin" /> : tenants.length === 0 ? <div className="empty">No tenants yet — create one above.</div> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {tenants.map((t) => (
                  <div key={t.tenant_id} style={{ border: "1px solid var(--line)", borderRadius: 8, padding: "10px 12px" }}>
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontWeight: 600 }}>{t.name || t.slug} <span className="mono sub">· {t.slug}</span></div>
                        <div className="sub">{t.projects} project{t.projects === 1 ? "" : "s"} · {t.active_keys} active key{t.active_keys === 1 ? "" : "s"}</div>
                      </div>
                      <button className="btn sm" onClick={() => login(t.slug)} disabled={loginBusy !== null}
                        title="Enter the Studio as this tenant (mints a hidden console-login key)">
                        {loginBusy === t.slug ? "Logging in…" : "Log in →"}
                      </button>
                      <button className="btn ghost sm" onClick={() => setExpanded(expanded === t.slug ? null : t.slug)}>{expanded === t.slug ? "Hide keys" : "Manage keys"}</button>
                      {confirmDel === t.slug
                        ? <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                            <span className="sub" style={{ color: "var(--crit)" }}>delete {t.slug}?</span>
                            <button className="btn sm" style={{ background: "var(--crit)", color: "#fff" }} onClick={() => del(t.slug)}>Yes, delete</button>
                            <button className="btn ghost sm" onClick={() => setConfirmDel(null)}>Cancel</button>
                          </span>
                        : <button className="btn ghost sm" onClick={() => setConfirmDel(t.slug)}>Delete</button>}
                    </div>
                    {expanded === t.slug && (
                      <>
                        <KeysPanel slug={t.slug} onReveal={(title, key) => setReveal({ title, apiKey: key })} onKeysChanged={load} />
                        <ProjectsPanel slug={t.slug} />
                      </>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// Admin-token gate → Console.
export default function AdminConsole({ onExit, onLogin }: { onExit: () => void; onLogin: (key: string, project: string) => void }) {
  const [authed, setAuthed] = useState(false);
  const [token, setToken] = useState(getAdminToken());
  const [checking, setChecking] = useState(Boolean(getAdminToken()));
  const [error, setError] = useState("");

  // If a token is already stored, validate it silently on mount.
  useEffect(() => {
    if (!getAdminToken()) { setChecking(false); return; }
    adminApi("GET", "/admin/tenants").then(() => setAuthed(true))
      .catch(() => { clearAdminToken(); setToken(""); })
      .finally(() => setChecking(false));
  }, []);

  const enter = async () => {
    setChecking(true); setError("");
    setAdminToken(token.trim());
    try { await adminApi("GET", "/admin/tenants"); setAuthed(true); }
    catch (e) {
      clearAdminToken();
      setError(e instanceof ApiError && e.status === 403 ? "Admin token rejected (403)." : `Cannot reach the admin API: ${e}`);
    } finally { setChecking(false); }
  };

  if (authed) return <Console onExit={() => { setAuthed(false); onExit(); }} onLogin={onLogin} />;

  return (
    <div className="sopilot">
      <div className="connect">
        <div className="card" style={{ width: 440 }}>
          {error && <span className="stripe crit" />}
          <div className="chead" style={{ paddingBottom: 0 }}><h3>Platform admin</h3></div>
          <div className="cbody" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <p style={{ margin: 0, color: "var(--muted)", fontSize: 13 }}>
              Enter the deployment admin token (<span className="mono">SOPILOT_ADMIN_TOKEN</span>) to manage tenants and API keys. Not a tenant key.
            </p>
            <input className="qinput mono" type="password" placeholder="admin token" value={token}
              onChange={(e) => setToken(e.target.value)} onKeyDown={(e) => e.key === "Enter" && token.trim() && enter()} />
            {error && <span className="chip crit" style={{ whiteSpace: "normal" }}><span className="cd" />{error}</span>}
            <button className="btn primary" disabled={!token.trim() || checking} onClick={enter}>{checking ? "Checking…" : "Enter"}</button>
            <button className="btn ghost sm" onClick={onExit}>← Back to tenant login</button>
          </div>
        </div>
      </div>
    </div>
  );
}
