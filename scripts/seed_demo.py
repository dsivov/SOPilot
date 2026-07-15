#!/usr/bin/env python3
"""Provision the demo tenant with the packaged examples — idempotent, safe to
re-run, works on a fresh deployment. This is the "show the prod team basic
functionality" button.

Seeds:
  - tenant `demo` / project `main` (subsystems=both)
  - prompt block `compliance.recording` (published)
  - SOP "Overdue Invoice Recovery"  ← docs/examples/overdue_invoice_recovery_sop.txt
  - SOP "Appointment Scheduling & Triage" ← docs/examples/appointment_scheduling_sop.pdf
    (with the compliance block bound to its greeting stage), both published
  - two short showcase conversations (so Sessions / Dashboard / audit have data)

Usage:  .venv/bin/python ../scripts/seed_demo.py [base_url]
Prints the connect credentials at the end. Requires the API up and
SOPILOT_ADMIN_TOKEN + OPENAI_API_KEY configured server-side.
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
ROOT = Path(__file__).resolve().parents[1]
ADMIN_TOKEN = os.environ.get("SOPILOT_ADMIN_TOKEN", "dev-admin-token-p0")


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:300]}


def upload(path: str, filepath: Path, name_hint: str, headers: dict) -> dict:
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"name_hint\"\r\n\r\n{name_hint}\r\n".encode()
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filepath.name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + filepath.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    key = os.environ.get("SOPILOT_DEMO_KEY", "")
    t = call("POST", "/admin/tenants", {"slug": "demo", "name": "Demo tenant"}, {"X-Admin-Token": ADMIN_TOKEN})
    if "api_key" in t:
        key = t["api_key"]
        print(f"created tenant demo (key below)")
    elif not key:
        print("tenant 'demo' already exists — pass its key via SOPILOT_DEMO_KEY to reseed. Aborting.")
        return 1
    auth = {"Authorization": f"Bearer {key}", "X-Project": "main"}
    call("POST", "/admin/projects", {"slug": "main", "subsystems": "both"}, auth)  # 409 if exists — fine

    existing = {s["name"]: s["id"] for s in call("GET", "/sops", None, auth) if isinstance(s, dict)}

    # prompt block (idempotent: re-save + publish is a new version, harmless)
    call("POST", "/prompt-blocks", {
        "name": "compliance.recording", "kind": "compliance",
        "content": "You must state early in the call: \"This call may be recorded for quality and training purposes.\"",
    }, auth)
    call("POST", "/prompt-blocks/compliance.recording/publish", None, auth)
    print("prompt block compliance.recording published")

    # SOP 1: collections (pasted text)
    if "Overdue Invoice Recovery" not in existing:
        text = (ROOT / "docs/examples/overdue_invoice_recovery_sop.txt").read_text()
        r = call("POST", "/sops/ingest", {"text": text, "name_hint": "Overdue Invoice Recovery"}, auth)
        existing[r["name"]] = r["id"]
        print(f"ingested: {r['name']} (lint clean: {r['lint']['publishable']})")

    # SOP 2: appointment scheduling (PDF upload) + bind the compliance block
    if "Appointment Scheduling & Triage" not in existing:
        r = upload("/sops/ingest-file", ROOT / "docs/examples/appointment_scheduling_sop.pdf",
                   "Appointment Scheduling & Triage", {k: v for k, v in auth.items()})
        existing[r["name"]] = r["id"]
        d = r["definition"]
        greet = next((a for a in d["agent_actions"] if "greet" in a["name"].lower()), d["agent_actions"][0])
        greet.setdefault("prompt_blocks", [])
        if "compliance.recording" not in greet["prompt_blocks"]:
            greet["prompt_blocks"].append("compliance.recording")
        call("PUT", f"/sops/{r['id']}", {"definition": d}, auth)
        print(f"ingested: {r['name']} (compliance block bound to {greet['name']})")

    # publish everything (lint-gated; report problems instead of failing silently)
    for name, sid in existing.items():
        pub = call("POST", f"/sops/{sid}/publish", None, auth)
        if "_status" in pub:
            print(f"PUBLISH BLOCKED for {name}: {pub['_body']}")
        else:
            print(f"published: {name} v{pub['version']}")

    # showcase conversations (text channel) so the console has data
    appt = existing.get("Appointment Scheduling & Triage")
    if appt:
        script = [
            "Hi, I'd like to book a check-up appointment please.",
            "Date of birth 4 April 1990, patient ID MH-11223.",
            "Tuesday morning works. Yes, book it - confirmed, thank you.",
        ]
        for i in range(2):
            sess = call("POST", "/sessions", {"sop_id": appt}, auth).get("session_id")
            if not sess:
                break
            outcome = "abandoned"
            for msg in script:
                r = call("POST", f"/sessions/{sess}/converse", {"user_message": msg}, auth)
                if r.get("terminal"):
                    outcome = r["terminal"]
                    break
                time.sleep(2)
            call("POST", f"/sessions/{sess}/outcome", {"outcome": outcome}, auth)
            call("POST", f"/sessions/{sess}/end", None, auth)
            print(f"showcase conversation {i + 1}: {outcome}")

    print("\n=== DEMO READY ===")
    print(f"API key : {key}")
    print("Project : main")
    print(f"Studio  : https://<host>:5174/?key={key}&project=main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
