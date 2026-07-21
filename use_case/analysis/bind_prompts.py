#!/usr/bin/env python3
"""Bind (or unbind) the approved-wording prompt blocks across all AENA SOP
stages. Reproducible so the binding is versioned, and so the prompt-impact
measurement can toggle it. Usage: bind_prompts.py [bind|unbind]"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else "bind"
KEYS = Path(__file__).parent.parent.parent / "TENANT_KEYS.local.txt"
import re
KEY = re.search(r"^TENANT:\s*aena\b.*?\n\s*key:\s*(sop_[a-f0-9]+)", KEYS.read_text(), re.M | re.S).group(1)
H = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "X-Project": "malaga"}


def call(m, p, b=None):
    r = urllib.request.Request("http://127.0.0.1:8100" + p,
                               data=json.dumps(b).encode() if b is not None else None, method=m, headers=H)
    try:
        return json.loads(urllib.request.urlopen(r, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:200]}


CFG = json.load((Path(__file__).parent.parent / "onboarding/aena.json").open())
BLOCKS = json.loads((Path(__file__).parent.parent.parent / CFG["prompts_file"]).read_text())
RULES = CFG.get("prompt_bindings", [])


def blocks_for(stage: str, sop: str) -> list[str]:
    nl, sl, out = stage.lower(), sop.lower(), []
    for r in RULES:
        soms = r.get("sop_matches")
        if soms and not any(m.lower() in sl for m in soms):
            continue
        if any(m.lower() in nl for m in r.get("stage_matches", [])):
            out += r.get("blocks", [])
    return sorted(set(out))


def main():
    if MODE == "bind":
        for b in BLOCKS:
            call("POST", "/prompt-blocks", {"name": b["name"], "kind": b.get("kind", "stage"), "content": b["content"]})
            call("POST", f"/prompt-blocks/{b['name']}/publish")
    total = 0
    for s in call("GET", "/sops"):
        d = call("GET", f"/sops/{s['id']}")["definition"]
        for a in d["agent_actions"]:
            a["prompt_blocks"] = blocks_for(a["name"], s["name"]) if MODE == "bind" else []
        call("PUT", f"/sops/{s['id']}", {"definition": d})
        pub = call("POST", f"/sops/{s['id']}/publish")
        n = sum(len(a.get("prompt_blocks") or []) for a in d["agent_actions"])
        total += n
        print(f"  {s['name'].split(chr(8212))[0].strip()[:34]:34} v{pub.get('version','?')} — {n} bindings")
    print(f"[{MODE}] {total} total stage bindings (blocks+rules from onboarding/aena.json)")


if __name__ == "__main__":
    main()
