#!/usr/bin/env python3
"""Form → SOP pipeline (PolarTie SmartForm, chosen approach).

Turns a pt-forms fields.json into a **form SOP** stored in SOPilot:
  - each dependency-cohesive GROUP becomes a STAGE (an agent_action);
  - the stage's PLAYBOOK (its questions + show-if rules) is a PROMPT BLOCK the
    stage references (first-class, versioned, reviewable/editable in the Studio);
  - a main-flow prompt block holds the global "how to run the form" instructions;
  - a stage↔field MAP block (JSON) lets the deterministic driver resolve
    current-field → stage in O(1) (no prose parsing).

The SOP is STRUCTURE/STORAGE (versioned, Studio-visible). At runtime it's driven
deterministically (current field → its stage); a strong supervisor reads the
stage's playbook block; constraints.py enforces the gating. Edges are `both`
(natural order, no forced ordering — the user may jump anywhere).

Author sections are the base groups (verified dependency-cohesive); big sections
split by internal dependency clusters; tiny ones merge up.

Usage:
  python scripts/form_to_sop.py \
    --form "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json" \
    --name "Injured Worker Questionnaire" \
    --tenant polartie --project smartform --publish --reset
"""
import argparse
import json
import re
import ssl
import urllib.error
import urllib.request

TOKEN = re.compile(r"\{([^}]+)\}")
SPLIT_THRESHOLD = 24
TARGET = 16
MERGE_BELOW = 3


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


def stage_id(title: str, i: int) -> str:
    return f"s{i:02d}_{slugify(title)[:40]}"


def parse(form_path: str):
    fields = json.load(open(form_path))
    if isinstance(fields, dict):
        fields = fields.get("fields", [])
    sections, cur, byname = [], None, {}
    for i, f in enumerate(fields):
        ft, nm = f.get("FieldType", "Text"), f.get("FieldName", f"_{i}")
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
    flds = sec["fields"]
    if len(flds) <= SPLIT_THRESHOLD:
        return [flds]
    names = {f["name"] for f in flds}
    parent = {f["name"]: f["name"] for f in flds}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for f in flds:
        for g in TOKEN.findall(f["cond"]):
            if g in names:
                parent[find(g)] = find(f["name"])
    comps: dict = {}
    for f in flds:
        comps.setdefault(find(f["name"]), []).append(f)
    ordered = sorted(comps.values(), key=lambda c: min(x["order"] for x in c))
    stages, cur = [], []
    for comp in ordered:
        if cur and len(cur) + len(comp) > TARGET:
            stages.append(cur); cur = []
        cur.extend(sorted(comp, key=lambda x: x["order"]))
    if cur:
        stages.append(cur)
    return stages


def build_stages(sections):
    raw = []
    for sec in sections:
        parts = split_section(sec)
        for k, part in enumerate(parts):
            title = sec["title"] if len(parts) == 1 else f"{sec['title']} (part {k + 1})"
            raw.append({"title": title, "fields": part})
    merged = []
    for st in raw:
        if merged and len(st["fields"]) < MERGE_BELOW:
            merged[-1]["fields"].extend(st["fields"])
        else:
            merged.append(st)
    from collections import Counter
    counts = Counter()
    for st in merged:
        m = re.match(r"^(.*) \(part \d+\)$", st["title"])
        if m:
            counts[m.group(1)] += 1
    seen: dict = {}
    for st in merged:
        m = re.match(r"^(.*) \(part \d+\)$", st["title"])
        if m:
            b = m.group(1); seen[b] = seen.get(b, 0) + 1
            st["title"] = b if counts[b] == 1 else f"{b} (part {seen[b]})"
    return merged


def playbook(stage: dict) -> str:
    lines = [f"STAGE: {stage['title']}",
             "Ask each question in order. SKIP a question when its show-if condition is false given "
             "the answers so far (a skipped question is recorded as \"not applicable\"). Coerce the answer "
             "to the field's format; confirm long free-text, send short answers as-is.", "", "Questions:"]
    for f in stage["fields"]:
        bits = [f"- {f['name']} · {f['label'] or '(no label)'}"]
        if f["options"]:
            opts = list(f["options"].values()) if isinstance(f["options"], dict) else f["options"]
            bits.append(f"  [choices: {', '.join(map(str, opts))}]")
        if f["cond"]:
            bits.append(f"  [show if: {f['cond']}]")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def flow_text(form_name: str) -> str:
    return (
        f"You are the SUPERVISOR for the \"{form_name}\" form — a strong model preparing each turn for a "
        "minimal realtime voice agent that just speaks your instruction and captures the raw answer.\n\n"
        "How to run the form:\n"
        "- You are given the CURRENT STAGE (a group of questions) and its playbook, plus the answers so far.\n"
        "- Ask the stage's questions in order. A question is SKIPPED when its show-if is false (this is "
        "enforced deterministically — never surface a skipped question).\n"
        "- When every visible question in the stage is answered, move to the next stage.\n"
        "- The user may jump or go back to ANY stage/question at any time — honor it.\n"
        "- Coerce answers to the field's format; resolve relative dates against today; transliterate names to "
        "Latin; confirm long free-text before saving; send short answers as-is; 'don't know' → unknown.\n"
        "- Return ONE COMPACT instruction for the realtime agent: just the next question to ask (plus a "
        "requested-change note if any). No field ids, no metadata.\n"
        "- Language: mirror the user's language for what they hear; store values in English."
    )


def build_definition(name, stages, prefix):
    actions, nodes, edges, prev = [], [], [], None
    for i, st in enumerate(stages):
        sid = stage_id(st["title"], i)
        st["_id"], st["_block"] = sid, f"{prefix}.{sid}"
        actions.append({"name": sid, "description": st["title"], "prompt_blocks": [st["_block"]]})
        nodes.append(sid)
        if prev is not None:
            edges.append({"src": prev, "dst": sid, "direction": "both"})  # natural order, not a hard prereq
        prev = sid
    return {
        "name": name,
        "description": "Form SOP — stages = dependency-cohesive question groups; each stage's playbook is a "
                       "prompt block. Driven deterministically by the supervisor (see the __flow__ block).",
        "conversation_profile": {"agent_role": "form supervisor", "goal": f"Complete the {name} accurately."},
        "agent_actions": actions, "user_states": [], "data_dependencies": [],
        "sop": {"nodes": nodes, "edges": edges},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--admin-token", default="dev-admin-token-p0")
    ap.add_argument("--tenant", default="polartie")
    ap.add_argument("--project", default="smartform")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete existing SOPs + prompt blocks first")
    a = ap.parse_args()

    sections, byname = parse(a.form)
    stages = build_stages(sections)
    name = a.name or re.sub(r"^\s*\d+\.\s*", "", a.form.split("/")[-2] if "/" in a.form else "form")
    prefix = slugify(name)
    definition = build_definition(name, stages, prefix)
    print(f"'{name}' → {len(stages)} stages (from {len(sections)} sections, {len(byname)} questions)")

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
            return {"_http": e.code, "detail": e.read().decode()[:300]}

    key = req("POST", f"/admin/tenants/{a.tenant}/login-key").get("api_key")
    if not key:
        print("could not mint a key"); return 1

    if a.reset:
        for s in req("GET", "/sops", key=key) or []:
            req("DELETE", f"/sops/{s['id']}", key=key)
        for b in req("GET", "/prompt-blocks", key=key) or []:
            req("DELETE", f"/prompt-blocks/{b['name']}", key=key)
        print("reset: cleared existing SOPs + prompt blocks")

    def put_block(bname, content, kind="stage"):
        req("POST", "/prompt-blocks", {"name": bname, "content": content, "kind": kind}, key)
        req("POST", f"/prompt-blocks/{bname}/publish", {}, key)

    # 1) per-stage playbook blocks
    for st in stages:
        put_block(st["_block"], playbook(st), "stage")
    # 2) main-flow block
    flow_block = f"{prefix}.__flow__"
    put_block(flow_block, flow_text(name), "role")
    # 3) stage↔field map block (JSON) — the deterministic driver's index
    manifest = {
        "form": name, "flow_block": flow_block, "order": [st["_id"] for st in stages],
        "stages": {st["_id"]: {
            "title": st["title"], "block": st["_block"],
            # per-field detail so the deterministic driver can gate + label without prose parsing
            "fields": [{"name": f["name"], "label": f["label"], "cond": f["cond"]} for f in st["fields"]],
        } for st in stages},
    }
    map_block = f"{prefix}.__map__"
    put_block(map_block, json.dumps(manifest, indent=1), "stage")
    print(f"emitted {len(stages)} playbook blocks + flow ({flow_block}) + map ({map_block})")

    # 4) the SOP (references the stage blocks)
    lint = req("POST", "/sops/lint-definition", {"definition": definition}, key)
    print(f"lint: publishable={lint.get('publishable')} problems={lint.get('problems')}")
    if not lint.get("publishable"):
        return 1
    created = req("POST", "/sops", {"definition": definition}, key)
    sop_id = created.get("id")
    if created.get("_http") == 409:
        ex = [s for s in req("GET", "/sops", key=key) if s.get("name") == name]
        if ex:
            sop_id = ex[0]["id"]; req("PUT", f"/sops/{sop_id}", {"definition": definition}, key)
    if sop_id and a.publish:
        req("POST", f"/sops/{sop_id}/publish", {}, key)
    sops = req("GET", "/sops", key=key)
    blocks = req("GET", "/prompt-blocks", key=key)
    print(f"SOP id={sop_id} · tenant now has {len(sops)} SOP(s), {len(blocks)} prompt blocks")
    print(f"→ Studio: tenant '{a.tenant}', project '{a.project}' → SOPs + Prompt blocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
