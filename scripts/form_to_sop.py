#!/usr/bin/env python3
"""Form → SOP pipeline (PolarTie SmartForm, chosen approach).

Turns a pt-forms fields.json into a **form SOP** stored in SOPilot: each
dependency-cohesive GROUP becomes a STAGE (an agent_action) whose description is
its PLAYBOOK (the group's questions + their show-if rules). Author sections are
the base groups (they're already dependency-cohesive — 0 cross-section deps); big
sections are split by their internal dependency clusters; tiny ones merge up.

The SOP is used for STRUCTURE/STORAGE (versioned, Studio-visible); it is driven
deterministically at runtime (current field → its stage), with a strong
supervisor reading each stage's playbook — not the SOP classifier.

Usage:
  python scripts/form_to_sop.py \
    --form "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json" \
    --base http://127.0.0.1:8100 --admin-token dev-admin-token-p0 \
    --tenant polartie --project smartform --publish
"""
import argparse
import json
import re
import ssl
import urllib.error
import urllib.request

TOKEN = re.compile(r"\{([^}]+)\}")
SPLIT_THRESHOLD = 24      # sections larger than this are split
TARGET = 16               # soft cap per sub-stage
MERGE_BELOW = 3           # sections with fewer fields merge into the previous stage


def slug(s: str, i: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "stage"
    return f"s{i:02d}_{base}"


def parse(form_path: str):
    fields = json.load(open(form_path))
    if isinstance(fields, dict):
        fields = fields.get("fields", [])
    sections, cur = [], None
    byname = {}
    for i, f in enumerate(fields):
        ft = f.get("FieldType", "Text")
        nm = f.get("FieldName", f"_{i}")
        if ft == "Section":
            cur = {"title": str(f.get("FieldValue") or nm), "fields": []}
            sections.append(cur)
            continue
        rec = {"name": nm, "ftype": ft, "label": str(f.get("FieldNameAlt") or ""),
               "cond": f.get("FieldCondition") or "", "options": f.get("FieldOptions"), "order": i}
        byname[nm] = rec
        if cur is None:
            cur = {"title": "(prologue)", "fields": []}
            sections.append(cur)
        cur["fields"].append(rec)
    return sections, byname


def split_section(sec: dict) -> list[list[dict]]:
    """Split a big section into sub-stages by intra-section dependency clusters,
    preserving order and never breaking a cluster."""
    flds = sec["fields"]
    if len(flds) <= SPLIT_THRESHOLD:
        return [flds]
    names = {f["name"] for f in flds}
    parent = {f["name"]: f["name"] for f in flds}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for f in flds:                       # union each dependent with the gates it references
        for g in TOKEN.findall(f["cond"]):
            if g in names:
                union(g, f["name"])
    # components in order of their earliest field
    comps: dict[str, list[dict]] = {}
    for f in flds:
        comps.setdefault(find(f["name"]), []).append(f)
    ordered = sorted(comps.values(), key=lambda c: min(x["order"] for x in c))
    # greedy pack components into sub-stages up to TARGET
    stages, cur = [], []
    for comp in ordered:
        if cur and len(cur) + len(comp) > TARGET:
            stages.append(cur)
            cur = []
        cur.extend(sorted(comp, key=lambda x: x["order"]))
    if cur:
        stages.append(cur)
    return stages


def build_stages(sections):
    """Sections → ordered (title, [fields]) stages, with big split + tiny merged."""
    raw = []
    for sec in sections:
        parts = split_section(sec)
        for k, part in enumerate(parts):
            title = sec["title"] if len(parts) == 1 else f"{sec['title']} (part {k + 1})"
            raw.append({"title": title, "fields": part})
    # merge tiny stages into the previous one
    merged = []
    for st in raw:
        if merged and len(st["fields"]) < MERGE_BELOW:
            merged[-1]["fields"].extend(st["fields"])
        else:
            merged.append(st)
    # relabel "(part N)" contiguously per section (merges can leave gaps); drop it if only one remains
    from collections import Counter
    counts = Counter()
    for st in merged:
        m = re.match(r"^(.*) \(part \d+\)$", st["title"])
        if m:
            counts[m.group(1)] += 1
    seen: dict[str, int] = {}
    for st in merged:
        m = re.match(r"^(.*) \(part \d+\)$", st["title"])
        if m:
            b = m.group(1)
            seen[b] = seen.get(b, 0) + 1
            st["title"] = b if counts[b] == 1 else f"{b} (part {seen[b]})"
    return merged


def playbook(stage: dict) -> str:
    lines = [f"STAGE: {stage['title']}",
             "Ask each question in order. SKIP a question when its show-if condition is false "
             "given the answers collected so far (a skipped question is recorded as \"not applicable\"). "
             "Coerce the answer to the field's format; confirm long free-text, send short answers as-is.",
             "", "Questions:"]
    for f in stage["fields"]:
        bits = [f"- {f['name']} · {f['label'] or '(no label)'}"]
        if f["options"]:
            opts = list(f["options"].values()) if isinstance(f["options"], dict) else f["options"]
            bits.append(f"  [choices: {', '.join(map(str, opts))}]")
        if f["cond"]:
            bits.append(f"  [show if: {f['cond']}]")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def build_definition(name: str, stages: list[dict]) -> dict:
    actions, nodes, edges, prev = [], [], [], None
    for i, st in enumerate(stages):
        sid = slug(st["title"], i)
        actions.append({"name": sid, "description": playbook(st)})
        nodes.append(sid)
        if prev is not None:
            edges.append({"src": prev, "dst": sid, "direction": "forward"})
        prev = sid
    return {
        "name": name,
        "description": "Auto-generated form SOP: stages = dependency-cohesive question groups; "
                       "each stage's description is its playbook. Driven deterministically by the supervisor.",
        "agent_actions": actions, "user_states": [], "data_dependencies": [],
        "sop": {"nodes": nodes, "edges": edges},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--admin-token", default="dev-admin-token-p0")
    ap.add_argument("--tenant", default="polartie")
    ap.add_argument("--project", default="smartform")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()

    sections, byname = parse(a.form)
    stages = build_stages(sections)
    name = a.form.split("/")[-2] if "/" in a.form else "form"
    definition = build_definition(name, stages)
    print(f"stages: {len(stages)} (from {len(sections)} sections, {len(byname)} questions)")
    for i, st in enumerate(stages):
        print(f"  {i:>2}  {len(st['fields']):>3} fields  {st['title']}")

    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    base = a.base.rstrip("/")

    def req(method, path, body=None, key=None):
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"; h["X-Project"] = a.project
        if path.startswith("/admin"):
            h["X-Admin-Token"] = a.admin_token
        r = urllib.request.Request(base + path, method=method,
                                   data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            return json.loads(urllib.request.urlopen(r, timeout=60, context=ctx).read() or b"{}")
        except urllib.error.HTTPError as e:
            return {"_http": e.code, "detail": e.read().decode()[:400]}

    key = req("POST", f"/admin/tenants/{a.tenant}/login-key").get("api_key")
    if not key:
        print("could not mint a key"); return 1

    lint = req("POST", "/sops/lint-definition", {"definition": definition}, key)
    print(f"lint: publishable={lint.get('publishable')} problems={lint.get('problems')}")
    if not lint.get("publishable"):
        print("not publishable — aborting before create"); return 1

    created = req("POST", "/sops", {"definition": definition}, key)
    if created.get("_http") == 409:  # exists → update
        existing = [s for s in req("GET", "/sops", key=key) if s.get("name") == name]
        sid = existing[0]["id"] if existing else None
        created = req("PUT", f"/sops/{sid}", {"definition": definition}, key) if sid else created
    sop_id = created.get("id")
    print(f"saved SOP id={sop_id} ({created if not sop_id else 'ok'})")
    if sop_id and a.publish:
        pub = req("POST", f"/sops/{sop_id}/publish", {}, key)
        print(f"published: {pub}")
    sops = req("GET", "/sops", key=key)
    print(f"verify: tenant now has {len(sops)} SOP(s): {[s.get('name') for s in sops]}")
    print(f"→ open the Studio as tenant '{a.tenant}', project '{a.project}' → SOPs to see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
