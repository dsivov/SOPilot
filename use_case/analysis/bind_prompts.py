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


BLOCKS = [
    ("greeting.welcome", "role", "Open with a brief, warm welcome to Málaga Airport information and invite the traveller to state their need. Mirror their language. E.g. «¡Bienvenido a información del aeropuerto de Málaga! ¿En qué puedo ayudarle?» / “Welcome to Málaga Airport information — how can I help you?”"),
    ("intake.clarify", "stage", "Pin down exactly what the traveller needs with one short, specific question before proceeding. E.g. «¿Me puede decir el número de vuelo o la aerolínea?» / “Could you tell me the flight number or the airline?”"),
    ("verify.confirm", "stage", "Briefly read back the key detail you will act on, so any mistake surfaces before you answer. E.g. «Entonces es el vuelo a Londres, ¿correcto?» / “So that's the flight to London — is that right?”"),
    ("directions.confirm", "stage", "After giving directions, confirm the traveller has understood and offer to repeat them. E.g. «¿Le queda claro el camino, o se lo repito?» / “Is the way clear, or shall I repeat it?”"),
    ("close.anything_else", "stage", "Close warmly and check nothing is left open. E.g. «¿Le puedo ayudar en algo más? ¡Buen viaje!» / “Is there anything else I can help with? Have a good trip!”"),
    ("empathy.delayed_bag", "stage", "A missing or delayed bag is stressful. Acknowledge it briefly and reassuringly before asking for details. E.g. «Entiendo, no se preocupe, vamos a localizarla.» / “I understand — don't worry, we'll track it down.”"),
    ("assistance.reassure", "stage", "For reduced-mobility or special-assistance requests, reassure the traveller that help is available and point them to (or offer to notify) the assistance desk. E.g. «La asistencia está disponible; le indico dónde solicitarla.» / “Assistance is available — let me show you where to request it.”"),
]
EXTRA = {"Lost or Delayed Luggage": "empathy.delayed_bag", "Airport Services": "assistance.reassure"}


def blocks_for(stage: str, sop: str) -> list[str]:
    nl, out = stage.lower(), []
    if "greet" in nl:
        out.append("greeting.welcome")
    if "identif" in nl:
        out.append("intake.clarify")
        out += [v for k, v in EXTRA.items() if sop.startswith(k)]
    if "verif" in nl:
        out.append("verify.confirm")
    if any(k in nl for k in ("provide", "resolve", "route", "inform")):
        out.append("directions.confirm")
        if "resolve" in nl and sop.startswith("Airport Services"):
            out.append("assistance.reassure")
    if "clos" in nl:
        out.append("close.anything_else")
    return out


def main():
    if MODE == "bind":
        for name, kind, content in BLOCKS:
            call("POST", "/prompt-blocks", {"name": name, "kind": kind, "content": content})
            call("POST", f"/prompt-blocks/{name}/publish")
    total = 0
    for s in call("GET", "/sops"):
        d = call("GET", f"/sops/{s['id']}")["definition"]
        for a in d["agent_actions"]:
            a["prompt_blocks"] = sorted(set(blocks_for(a["name"], s["name"]))) if MODE == "bind" else []
        call("PUT", f"/sops/{s['id']}", {"definition": d})
        p = call("POST", f"/sops/{s['id']}/publish")
        n = sum(len(a.get("prompt_blocks") or []) for a in d["agent_actions"])
        total += n
        print(f"  {s['name'].split('—')[0].strip()[:34]:34} v{p.get('version','?')} — {n} bindings")
    print(f"[{MODE}] {total} total stage bindings")


if __name__ == "__main__":
    main()
