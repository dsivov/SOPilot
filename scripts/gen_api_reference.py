#!/usr/bin/env python3
"""Generate docs/API_REFERENCE.md from the live OpenAPI spec (authoritative,
always-current). Run against a running server: python scripts/gen_api_reference.py
[base_url]. Groups endpoints by tag/prefix, lists auth, params, request/response."""
from __future__ import annotations
import json, sys, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
spec = json.loads(urllib.request.urlopen(BASE + "/openapi.json", timeout=15).read())
schemas = spec.get("components", {}).get("schemas", {})

GROUPS = [
    ("Health", ["/health"]),
    ("Admin — tenants & projects", ["/admin"]),
    ("SOP authoring", ["/sops"]),
    ("Prompt blocks", ["/prompt-blocks"]),
    ("Connectors (retrieval systems)", ["/connectors"]),
    ("Corpora (managed knowledge)", ["/corpora"]),
    ("Tenant secrets", ["/secrets"]),
    ("Sessions & conversations", ["/sessions"]),
    ("Precedent traces", ["/traces"]),
    ("A/B autopilot", ["/abtests"]),
    ("Metrics", ["/metrics"]),
]

def group_of(path):
    for name, prefixes in GROUPS:
        if any(path == p or path.startswith(p + "/") or path.startswith(p + "{") for p in prefixes):
            return name
    return "Other"

def ref_name(schema):
    if "$ref" in schema: return schema["$ref"].split("/")[-1]
    if schema.get("type") == "array" and "$ref" in schema.get("items", {}):
        return schema["items"]["$ref"].split("/")[-1] + "[]"
    return schema.get("type", "object")

def props_table(name):
    s = schemas.get(name.rstrip("[]"))
    if not s or "properties" not in s: return ""
    req = set(s.get("required", []))
    lines = ["\n| field | type | required | notes |", "|---|---|---|---|"]
    for f, meta in s["properties"].items():
        t = meta.get("type", ref_name(meta) if "$ref" in meta or "items" in meta else "any")
        if meta.get("enum"): t = " \\| ".join(f'`{e}`' for e in meta["enum"])
        note = meta.get("description", "")
        if "default" in meta: note = (note + f" (default `{meta['default']}`)").strip()
        lines.append(f"| `{f}` | {t} | {'yes' if f in req else ''} | {note} |")
    return "\n".join(lines) + "\n"

grouped = {}
for path, ops in spec["paths"].items():
    for method, op in ops.items():
        grouped.setdefault(group_of(path), []).append((path, method.upper(), op))

out = ["# SOPilot API Reference\n",
    f"Complete endpoint reference — **{sum(len(v) for v in grouped.values())} endpoints**, generated from the "
    "live OpenAPI spec (`docs/openapi.json`). Regenerate with `python scripts/gen_api_reference.py`. The "
    "interactive version is served at `/docs` (Swagger UI) on any running instance; task-oriented flows are in "
    "[`INTEGRATION.md`](INTEGRATION.md).\n",
    "## Authentication\n",
    "- **Admin plane** (`/admin/tenants`): header `X-Admin-Token: <SOPILOT_ADMIN_TOKEN>`.\n"
    "- **Everything else**: `Authorization: Bearer sop_<key>` + `X-Project: <slug>` on project-scoped routes. "
    "Keys are tenant-scoped.\n- Errors: `401` bad key · `404` unknown/cross-tenant · `409` state conflict · "
    "`422` validation/lint (`problems[]`) · `429` quota.\n"]

order = [g[0] for g in GROUPS] + ["Other"]
for gname in order:
    if gname not in grouped: continue
    out.append(f"\n## {gname}\n")
    for path, method, op in sorted(grouped[gname], key=lambda x: (x[0], x[1])):
        out.append(f"### `{method} {path}`\n")
        if op.get("summary") or op.get("description"):
            out.append((op.get("description") or op.get("summary")).strip() + "\n")
        params = op.get("parameters", [])
        if params:
            out.append("**Parameters:** " + ", ".join(
                f"`{p['name']}` ({p['in']}{', required' if p.get('required') else ''})" for p in params) + "\n")
        rb = op.get("requestBody", {}).get("content", {})
        for ct, media in rb.items():
            rn = ref_name(media.get("schema", {}))
            out.append(f"**Request body** (`{ct}`): `{rn}`" + props_table(rn))
        resp = op.get("responses", {})
        codes = ", ".join(f"`{c}`" for c in resp if c != "422")
        out.append(f"**Responses:** {codes}\n")
open("docs/API_REFERENCE.md", "w").write("\n".join(out))
print(f"docs/API_REFERENCE.md — {sum(len(v) for v in grouped.values())} endpoints, {len(grouped)} groups")
