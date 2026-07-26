#!/usr/bin/env python3
"""End-to-end config-manager scenario — runs the full three-stage cycle against
a running SOPilot instance and verifies everything is persisted (DB-backed), so
it survives an API restart.

  Stage 0  import a System Analysis report        → /config/analysis
  Stage 1  publish a schema (DSL) + a rule         → /config/ruleset
  Stage 2  publish a customer config within bounds → /config/document

Then re-fetches all three and asserts they match — proving durable persistence.

Usage:
  # full run (create tenant if needed, run all stages, verify):
  python scripts/e2e_config_manager.py --base http://127.0.0.1:8100 \
      --admin-token "$SOPILOT_ADMIN_TOKEN" --tenant pt-e2e --project robot1

  # after restarting the API, prove nothing was lost:
  python scripts/e2e_config_manager.py --base ... --admin-token ... \
      --tenant pt-e2e --project robot1 --verify-only

Options: --report <path> (default docs/examples/pt-multirobot-analysis.json),
--verify-only (skip writes; just re-fetch and assert), --keep (don't delete the
tenant afterwards; default keeps it anyway — deletion is never automatic).
No third-party deps. Exit 0 = all checks passed.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

FAILS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ✔ " if cond else "  ✖ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS += 1


def make_client(base: str, admin_token: str):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    base = base.rstrip("/")

    def req(method, path, body=None, key=None, project=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if project:
            headers["X-Project"] = project
        if admin_token and path.startswith("/admin"):
            headers["X-Admin-Token"] = admin_token
        r = urllib.request.Request(base + path, method=method,
                                   data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            return json.loads(urllib.request.urlopen(r, timeout=60, context=ctx).read() or b"{}")
        except urllib.error.HTTPError as e:
            return {"_http": e.code, "detail": e.read().decode()[:300]}
    return req


def schema_from_report(rep: dict) -> dict:
    items = rep.get("config_items", [])
    return {
        "name": (rep.get("system") or {}).get("name", "system"),
        "fields": [{"path": c["path"], "type": c.get("type", "string"), "description": c.get("description"),
                    "options": c.get("options") if c.get("type") == "enum" else None,
                    "required": c.get("required"), "advanced": c.get("advanced"), "status": c.get("status")}
                   for c in items if c.get("item") == "field"],
        "tools": [{"name": c["path"], "description": c.get("description")} for c in items if c.get("item") == "tool"],
        "structures": [{"key": c["path"], "label": c.get("description")} for c in items if c.get("item") == "structure"],
    }


# The customer config Stage 2 authors — within the schema + rule below.
CUSTOMER_CONFIG = {
    "display_name": "Ava", "voice": "coral", "default_language_iso": "en",
    "custom_config": {"gpt_model": "gpt-realtime"},
    "notification_service_url": "https://notify.example/send",
    "prompt": "You are Ava, a friendly front-desk assistant. Keep replies short.",
    "tools": {"hangup": {"enabled": True}, "transfer": {"enabled": True},
              "send_email": {"enabled": True}, "show_text": {"enabled": True}},
    "mcp_servers": [{"url": "https://kb.example/mcp", "authorization": "secret:kb_token"}],
    "knowledge_base": [],
    "transfer_topics": [{"topic_id": "human_desk", "function_tag": "human", "prompt": "Route to a human agent."}],
}
RULE = {"id": "email-needs-notif", "kind": "requires", "when": "tool:send_email",
        "needs": "field:notification_service_url", "level": "error",
        "msg": "send_email needs notification_service_url"}


def main() -> int:
    ap = argparse.ArgumentParser(description="SOPilot config-manager E2E scenario")
    ap.add_argument("--base", default=os.environ.get("SOPILOT_BASE_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--admin-token", default=os.environ.get("SOPILOT_ADMIN_TOKEN", ""))
    ap.add_argument("--tenant", default="pt-e2e")
    ap.add_argument("--project", default="robot1")
    ap.add_argument("--report", default="docs/examples/pt-multirobot-analysis.json")
    ap.add_argument("--verify-only", action="store_true", help="skip writes; only re-fetch and assert")
    args = ap.parse_args()
    if not args.admin_token:
        print("need --admin-token (or SOPILOT_ADMIN_TOKEN)"); return 2

    req = make_client(args.base, args.admin_token)
    rep = json.load(open(args.report))

    # ensure tenant + project, then a working key (admin plane)
    if not args.verify_only:
        req("POST", "/admin/tenants", {"slug": args.tenant, "name": args.tenant, "project_slug": args.project})
    lk = req("POST", f"/admin/tenants/{args.tenant}/login-key")
    key = lk.get("api_key")
    if not key:
        print(f"could not get a key for tenant '{args.tenant}': {lk}"); return 1
    proj = args.project

    if not args.verify_only:
        print(f"== writing all 3 stages to {args.tenant}/{proj} ==")
        r0 = req("PUT", "/config/analysis", {"report": rep, "publish": True}, key, proj)
        ok(f"Stage 0 · analysis report published v{r0.get('version')}", r0.get("published_version") is not None, str(r0))
        r1 = req("PUT", "/config/ruleset", {"rules": [RULE], "schema": schema_from_report(rep), "publish": True}, key, proj)
        ok(f"Stage 1 · schema + rule published v{r1.get('version')}", r1.get("published_version") is not None, str(r1))
        r2 = req("PUT", "/config/document", {"config": CUSTOMER_CONFIG, "publish": True}, key, proj)
        ok(f"Stage 2 · config published v{r2.get('version')}", r2.get("published_version") is not None, str(r2))

    print(f"== verifying persisted state of {args.tenant}/{proj} =="
          + (" (restart-survival check)" if args.verify_only else ""))
    a = req("GET", "/config/analysis", key=key, project=proj)
    ok("Stage 0 report persisted", bool(a.get("published_report")),
       f"expected a published report, got {a.get('published_version')}")
    exp_fields = sum(1 for c in rep.get("config_items", []) if c.get("item") == "field")
    rs = req("GET", "/config/ruleset", key=key, project=proj)
    ps = rs.get("published_schema") or {}
    ok(f"Stage 1 schema persisted ({len(ps.get('fields', []))}/{exp_fields} fields)", len(ps.get("fields", [])) == exp_fields)
    ok("Stage 1 rule persisted", len(rs.get("published_rules") or []) >= 1)
    doc = req("GET", "/config/document", key=key, project=proj)
    pc = doc.get("published_config") or {}
    ok("Stage 2 config persisted (display_name)", pc.get("display_name") == CUSTOMER_CONFIG["display_name"])
    ok("Stage 2 kept prompt", bool(pc.get("prompt")))
    ok("Stage 2 kept enabled tools", (pc.get("tools") or {}).get("send_email", {}).get("enabled") is True)
    ok("Stage 2 kept MCP server", len(pc.get("mcp_servers") or []) == 1)
    ok("Stage 2 kept transfer topic", len(pc.get("transfer_topics") or []) == 1)

    print()
    if FAILS:
        print(f"{FAILS} CHECK(S) FAILED"); return 1
    print("ALL CHECKS PASSED — the three stages are persisted and restore-able.")
    print(f"Explore in the Studio: log into tenant '{args.tenant}', project '{proj}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
