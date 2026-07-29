#!/usr/bin/env python3
"""Faithful mock of pt-forms `get-fields`/`set-fields` for the SmartForm live spike.

Serves the EXACT contract the real Laravel McpController does (verified against
source): id-keyed indexed schema (FieldName stripped, {FieldName}->id resolved in
FieldCondition) + id-keyed `values`, guarded by X-Browser-Session-Token. The real
PolarTie stack (Laravel/MySQL/Redis) isn't stood up in this environment; this lets
us exercise SOPilot's live /formflow/prepare path against a conformant source.

  GET /api/fill/{uuid}/get-fields   -> {success, fields:[{id,...}], values:{id:val}, ...}
  PUT /api/fill/{uuid}/set-fields   -> merge id-keyed values (the realtime agent writing answers)
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = re.compile(r"\{([^}]+)\}")
FORM = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
UUID = "spike-submission-0001"
SESSION_TOKEN = "spike-token"

# ---- build the indexed schema exactly like pt-forms getIndexedSchema ----
raw = json.load(open(FORM))
raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
name_to_id, fid = {}, 0
for f in raw:
    if "FieldName" in f:
        fid += 1
        name_to_id[f["FieldName"]] = fid


def resolve(text: str) -> str:
    return TOKEN.sub(lambda m: "{%s}" % name_to_id.get(m.group(1), m.group(1)), text or "")


INDEXED = []
for i, f in enumerate(raw):
    e = {k: v for k, v in f.items() if k not in ("FieldName", "FieldFlags", "FieldJustification")}
    if "FieldName" in f:
        e["id"] = name_to_id[f["FieldName"]]
    if isinstance(e.get("FieldCondition"), str):
        e["FieldCondition"] = resolve(e["FieldCondition"])
    INDEXED.append(e)

VALUES: dict[str, object] = {}   # id-keyed answer store (mutable via set-fields)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth_ok(self):
        return self.headers.get("X-Browser-Session-Token") == SESSION_TOKEN

    def do_GET(self):
        m = re.match(r"^/api/fill/([^/]+)/get-fields$", self.path)
        if not m:
            return self._send(404, {"success": False, "message": "not found"})
        if m.group(1) != UUID:
            return self._send(404, {"success": False, "message": "Submission not found"})
        if not self._auth_ok():
            return self._send(403, {"success": False, "message": "Invalid browser session"})
        self._send(200, {"success": True, "form_uuid": "spike-form",
                         "fields": INDEXED, "form_rules": None, "values": dict(VALUES),
                         "language": "en", "verification": {"required": False}})

    def do_PUT(self):
        m = re.match(r"^/api/fill/([^/]+)/set-fields$", self.path)
        if not m or m.group(1) != UUID:
            return self._send(404, {"success": False, "message": "Submission not found"})
        if not self._auth_ok():
            return self._send(403, {"success": False, "message": "Invalid browser session"})
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        if payload.get("__reset__"):          # test helper: clear the answer store
            VALUES.clear()
            return self._send(200, {"success": True, "reset": True})
        for k, v in (payload.get("values") or payload.get("data") or payload).items():
            VALUES[str(k)] = v
        self._send(200, {"success": True, "count": len(VALUES)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9700
    print(f"mock pt-forms on :{port} — {len(INDEXED)} fields, {fid} ids, uuid={UUID}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
