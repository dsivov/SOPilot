#!/usr/bin/env python3
"""SOPilot onboarding orchestrator — the mechanical stages of the productization
recipe, driven by one per-customer config file (see docs/ONBOARDING.md).

This automates the provisioning glue that used to be copy-pasted per customer;
the domain-specific analysis (survey/clean/intents/mining) stays in the
use_case/analysis toolkit, and the human-judgment gates (PII decision, topic
selection, SOP review, go/no-go) are the operator's, not this script's.

Usage:
    python onboard.py <config.json> provision     # tenant, project, ingest+publish SOPs
    python onboard.py <config.json> knowledge      # create corpus + load facts (or push to Context Graph)
    python onboard.py <config.json> connectors     # create connectors + bind to answering stages
    python onboard.py <config.json> status         # what's provisioned right now
    python onboard.py <config.json> all            # provision -> knowledge -> connectors

Idempotent: safe to re-run. Requires the API up (base_url) and, for a new
tenant, SOPILOT_ADMIN_TOKEN. The created API key is appended to
TENANT_KEYS.local.txt (gitignored) and reused on later runs.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Relative paths in the config resolve against the CURRENT WORKING DIRECTORY,
# so run onboard.py from the root those paths are relative to (the repo root,
# or the delivery bundle root). This keeps one script correct in both layouts.
ROOT = Path.cwd()
KEYS_FILE = ROOT / "TENANT_KEYS.local.txt"


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    for req in ("tenant_slug", "project_slug", "base_url"):
        if not cfg.get(req):
            sys.exit(f"config missing required field: {req}")
    return cfg


def resolve_key(cfg: dict) -> str | None:
    """API key from (1) env named in config, (2) TENANT_KEYS.local.txt block."""
    env = cfg.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env]
    if KEYS_FILE.exists():
        txt = KEYS_FILE.read_text()
        m = re.search(rf"^TENANT:\s*{re.escape(cfg['tenant_slug'])}\b.*?\n\s*key:\s*(sop_[a-f0-9]+)",
                      txt, re.MULTILINE | re.DOTALL)
        if m:
            return m.group(1)
    return None


def save_key(cfg: dict, key: str) -> None:
    block = (f"\nTENANT: {cfg['tenant_slug']:15} (onboarded via onboard.py)\n"
             f"  key:      {key}\n"
             f"  projects: {cfg['project_slug']}\n")
    with KEYS_FILE.open("a") as f:
        f.write(block)
    print(f"  → API key saved to {KEYS_FILE.name}")


def api(cfg: dict, method: str, path: str, body=None, *, key: str | None = None,
        admin: bool = False, multipart: tuple | None = None) -> dict:
    headers = {}
    if admin:
        headers["X-Admin-Token"] = os.environ.get(cfg.get("admin_token_env", "SOPILOT_ADMIN_TOKEN"), "")
    else:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Project"] = cfg["project_slug"]
    data = None
    if multipart is not None:
        boundary, data = multipart
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(cfg["base_url"] + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:300]}


# ---- helpers --------------------------------------------------------------

def stage_blocks(stage: str, sop: str, rules: list) -> list[str]:
    """Approved-wording blocks for a stage, from the config's `prompt_bindings`
    rules (14c). Each rule: {stage_matches:[substrings], blocks:[names],
    sop_matches?:[substrings]} — matched case-insensitively on stage/SOP name."""
    nl, sl, out = stage.lower(), sop.lower(), []
    for r in rules:
        soms = r.get("sop_matches")
        if soms and not any(m.lower() in sl for m in soms):
            continue
        if any(m.lower() in nl for m in r.get("stage_matches", [])):
            out += r.get("blocks", [])
    return sorted(set(out))


# ---- stages ---------------------------------------------------------------

def provision(cfg: dict) -> None:
    print(f"[provision] tenant={cfg['tenant_slug']} project={cfg['project_slug']}")
    key = resolve_key(cfg)
    if not key:
        r = api(cfg, "POST", "/admin/tenants",
                {"slug": cfg["tenant_slug"], "name": cfg.get("tenant_name", cfg["tenant_slug"])}, admin=True)
        if "api_key" in r:
            key = r["api_key"]
            print(f"  created tenant {cfg['tenant_slug']}")
            save_key(cfg, key)
        else:
            sys.exit(f"  tenant create failed and no key found: {r}")
    else:
        print("  tenant key resolved")

    r = api(cfg, "POST", "/admin/projects",
            {"slug": cfg["project_slug"], "subsystems": cfg.get("subsystems", "both")}, key=key)
    print(f"  project: {r if '_status' not in r else 'exists (ok)'}")

    # Prompt blocks first — SOP publish fails if a bound block has no published
    # version (D-7). Load + publish each before the SOPs that reference them.
    if cfg.get("prompts_file"):
        pf = ROOT / cfg["prompts_file"]
        if pf.exists():
            for b in json.loads(pf.read_text()):
                api(cfg, "POST", "/prompt-blocks",
                    {"name": b["name"], "kind": b.get("kind", "stage"), "content": b["content"]}, key=key)
                api(cfg, "POST", f"/prompt-blocks/{b['name']}/publish", key=key)
            print(f"  prompt blocks: {len(json.loads(pf.read_text()))} published")

    sops_dir = ROOT / cfg.get("sops_dir", "")
    existing = {s["name"]: s["id"] for s in api(cfg, "GET", "/sops", key=key) if isinstance(s, dict)}

    # Preferred: load exact published SOP DEFINITIONS from JSON (deterministic —
    # exactly what was validated). Fallback: re-ingest text through the LLM
    # (use only when no definitions are shipped; the graph may differ).
    rules = cfg.get("prompt_bindings") or []
    defs = sorted(sops_dir.glob("*.json"))
    if defs:
        for jf in defs:
            payload = json.loads(jf.read_text())
            definition = payload.get("definition", payload)
            name = definition.get("name", jf.stem)
            # 14c: apply the approved-wording placement declared in the config,
            # so the binding map lives in config (not baked into the SOP JSON).
            if rules:
                for a in definition["agent_actions"]:
                    b = stage_blocks(a["name"], name, rules)
                    a["prompt_blocks"] = sorted(set((a.get("prompt_blocks") or []) + b))
            if name in existing:
                api(cfg, "PUT", f"/sops/{existing[name]}", {"definition": definition}, key=key)
                sid = existing[name]
                print(f"  updated: {name[:50]}")
            else:
                r = api(cfg, "POST", "/sops", {"definition": definition}, key=key)
                if "_status" in r:
                    print(f"  LOAD FAILED {jf.name}: {r['_body']}")
                    continue
                sid = r["id"]
                print(f"  loaded: {name[:50]}")
            pub = api(cfg, "POST", f"/sops/{sid}/publish", key=key)
            print(f"    published v{pub.get('version', pub.get('_body', '?'))}")
    else:
        for txt in sorted(sops_dir.glob("sop_*.txt")):
            text = txt.read_text()
            name_hint = text.splitlines()[0][:200]
            if name_hint in existing:
                print(f"  SOP exists: {name_hint[:50]}")
                sid = existing[name_hint]
            else:
                r = api(cfg, "POST", "/sops/ingest", {"text": text, "name_hint": name_hint}, key=key)
                if "_status" in r:
                    print(f"  INGEST FAILED {txt.name}: {r['_body']}")
                    continue
                sid = r["id"]
                print(f"  ingested: {r['name'][:50]} (lint clean: {r['lint']['publishable']})")
            if rules:  # 14c: apply config-declared bindings to the ingested SOP
                d = api(cfg, "GET", f"/sops/{sid}", key=key)["definition"]
                for a in d["agent_actions"]:
                    b = stage_blocks(a["name"], d.get("name", ""), rules)
                    a["prompt_blocks"] = sorted(set((a.get("prompt_blocks") or []) + b))
                api(cfg, "PUT", f"/sops/{sid}", {"definition": d}, key=key)
            pub = api(cfg, "POST", f"/sops/{sid}/publish", key=key)
            print(f"    published v{pub.get('version', pub.get('_body', '?'))}")
    print("[provision] done")


def knowledge(cfg: dict) -> None:
    kn = cfg.get("knowledge", {})
    facts = [json.loads(ln) for ln in (ROOT / cfg["facts_file"]).open(encoding="utf-8")]
    print(f"[knowledge] {len(facts)} facts, mode={kn.get('mode')}")
    if kn.get("mode") == "context_graph":
        from collections import defaultdict
        by_topic = defaultdict(list)
        for f in facts:
            by_topic[f.get("topic", "general")].append(f["text"])
        texts = [f"{cfg['domain']['descriptor']} — {t}.\n\n" + "\n".join("- " + x for x in xs)
                 for t, xs in by_topic.items()]
        req = urllib.request.Request(
            kn["cg_url"].rsplit("/query", 1)[0] + "/documents/texts",
            data=json.dumps({"texts": texts, "file_sources": [f"{cfg['customer']}_{t}.txt" for t in by_topic]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "LIGHTRAG-WORKSPACE": kn["cg_workspace"]})
        print("  CG:", json.loads(urllib.request.urlopen(req, timeout=180).read()).get("status"))
        print("  → wait for CG pipeline to finish, then wire a connector of kind 'http' to its /query")
        return
    key = resolve_key(cfg)
    name = kn.get("corpus_name", "facts")
    print("  corpus:", api(cfg, "PUT", f"/corpora/{name}", key=key))
    docs = [{"doc_key": f["key"], "topic": f.get("topic", ""), "tags": [f.get("topic", "")], "text": f["text"]}
            for f in facts]
    for i in range(0, len(docs), 100):
        print("  ", api(cfg, "PUT", f"/corpora/{name}/docs", {"docs": docs[i:i + 100]}, key=key))
    print("[knowledge] done")


def connectors(cfg: dict) -> None:
    key = resolve_key(cfg)
    print("[connectors]")
    for c in cfg.get("connectors", []):
        r = api(cfg, "PUT", f"/connectors/{c['name']}",
                {"kind": c["kind"], "description": c.get("description", ""), "config": c.get("config", {})}, key=key)
        print(f"  {c['name']} ({c['kind']}): {r}")
    bind = cfg.get("bind_connector_to_answering_stages")
    if bind:
        kws = cfg.get("answering_stage_keywords", [])
        for s in api(cfg, "GET", "/sops", key=key):
            d = api(cfg, "GET", f"/sops/{s['id']}", key=key)["definition"]
            dep_name = "kb_" + bind.replace("-", "_")
            deps = {x["name"] for x in d.get("data_dependencies", [])}
            if dep_name not in deps:
                d.setdefault("data_dependencies", []).append({
                    "name": dep_name, "kind": "rag", "idempotent": True,
                    "config": {"connector": bind}, "query_template": "{user_text}"})
            for a in d["agent_actions"]:
                if any(k in a["name"].lower() for k in kws) and "clos" not in a["name"].lower():
                    a["data_dependencies"] = sorted(set((a.get("data_dependencies") or []) + [dep_name]))
            api(cfg, "PUT", f"/sops/{s['id']}", {"definition": d}, key=key)
            api(cfg, "POST", f"/sops/{s['id']}/publish", key=key)
            print(f"  bound '{bind}' into {s['name'][:40]}")
    print("[connectors] done")


def status(cfg: dict) -> None:
    key = resolve_key(cfg)
    if not key:
        print("not provisioned (no key)"); return
    sops = api(cfg, "GET", "/sops", key=key)
    cons = api(cfg, "GET", "/connectors", key=key)
    corp = api(cfg, "GET", "/corpora", key=key)
    print(f"tenant={cfg['tenant_slug']} project={cfg['project_slug']} ({cfg.get('subsystems')})")
    print(f"  SOPs ({len(sops)}):", [s["name"][:40] for s in sops])
    print(f"  connectors ({len(cons)}):", [(c["name"], c["kind"], f"{c['stats']['fetches']}f") for c in cons])
    print(f"  corpora ({len(corp)}):", [(c["name"], f"{c['docs']}docs") for c in corp])


STAGES = {"provision": provision, "knowledge": knowledge, "connectors": connectors, "status": status}


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[2] not in {*STAGES, "all"}:
        sys.exit(f"usage: onboard.py <config.json> [{'|'.join(STAGES)}|all]")
    cfg = load_config(sys.argv[1])
    stage = sys.argv[2]
    if stage == "all":
        provision(cfg); knowledge(cfg); connectors(cfg); status(cfg)
    else:
        STAGES[stage](cfg)


if __name__ == "__main__":
    main()
