#!/usr/bin/env python3
"""Head-to-head: backward edit-propagation — original pt-forms flow vs SOP + reconcile.

THE DEFECT: a field answered while visible can be HIDDEN by a later change to a
controlling answer. pt-forms' MCP leaves the stale answer in place
(_get_next_empty_field_id skips already-valued fields; the PDF generator excludes only
empty / "not applicable" values, never re-evaluating FieldConditions) — so it prints on
the PDF. This is the one place the two flows genuinely differ.

Each scenario: make a conditional field VISIBLE, answer it for real, then flip the
controlling answer so it becomes HIDDEN. Metric = stale answers that survive to output
(a real answer on a currently-hidden field). Both arms run against the same live mock
pt-forms; ground truth is the form's own show-if rules (constraints.py oracle).

  Arm ORIG = pt-forms native: nothing clears the stale answer -> it leaks.
  Arm SOP  = POST /formflow/reconcile (live): voids answered-but-hidden fields -> 0 leaks.
"""
import json
import re
import statistics as st
import sys
import time
import urllib.request

from sopilot.constraints import evaluate_condition

API = "http://127.0.0.1:8100"
MOCK = "http://127.0.0.1:9700"
UUID = "spike-submission-0001"
TOK = "spike-token"
SOURCE = {"base_url": MOCK, "submission_uuid": UUID, "browser_session_token": TOK}
FORM = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
TOKEN = re.compile(r"\{([^}]+)\}")


def post(u, b, h):
    r = urllib.request.Request(u, method="POST", data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json", **h})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())


def put(u, b, h):
    r = urllib.request.Request(u, method="PUT", data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json", **h})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())


def get(u, h):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=30).read())


# ---- parse the real form: fields with (name, id, cond) ----
def load_fields():
    raw = json.load(open(FORM))
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    fid, out, by = 0, [], {}
    for f in raw:
        if "FieldName" in f:
            fid += 1
        if f.get("FieldType") == "Section" or "FieldName" not in f:
            continue
        rec = {"name": f["FieldName"], "id": fid, "cond": f.get("FieldCondition") or "",
               "label": str(f.get("FieldNameAlt") or "")}
        out.append(rec); by[rec["name"]] = rec
    return out, by


FIELDS, BY_NAME = load_fields()
NAME_TO_ID = {f["name"]: f["id"] for f in FIELDS}


def satisfy_and_flip(cond):
    """Given a single-controller condition, return (controller, make_visible, make_hidden)
    values, or None if we can't cleanly build a scenario."""
    toks = TOKEN.findall(cond)
    if len(set(toks)) != 1:
        return None  # only single-controller conditions → clean, unambiguous scenarios
    ctrl = toks[0]
    c = cond.strip()
    if re.fullmatch(r"isYes\(\{%s\}\)" % re.escape(ctrl), c):
        return ctrl, "Yes", "No"
    if re.fullmatch(r"isNo\(\{%s\}\)" % re.escape(ctrl), c):
        return ctrl, "No", "Yes"
    m = re.fullmatch(r"values\[\{%s\}\]\s*(>=|>|<=|<|==|!=)\s*(-?\d+)" % re.escape(ctrl), c)
    if m:
        op, n = m.group(1), int(m.group(2))
        table = {">=": (n, n - 1), ">": (n + 1, n), "<=": (n, n + 1), "<": (n - 1, n),
                 "==": (n, n + 1), "!=": (n + 1, n)}
        vis, hid = table[op]
        return ctrl, str(vis), str(hid)
    return None


def leaks(answers):
    """Fields with a REAL answer that are currently HIDDEN (would print on the PDF)."""
    out = []
    for f in FIELDS:
        if not f["cond"]:
            continue
        v = answers.get(f["name"])
        if v is not None and str(v).strip().lower() not in ("", "not applicable", "n/a", "unknown") \
                and not evaluate_condition(f["cond"], answers):
            out.append(f["name"])
    return out


def build_scenarios(cap):
    scen = []
    for f in FIELDS:
        if not f["cond"]:
            continue
        sf = satisfy_and_flip(f["cond"])
        if not sf:
            continue
        ctrl, vis, hid = sf
        if ctrl not in BY_NAME:
            continue
        # post-edit answer set: dependent answered for real, controller flipped to HIDE it
        scen.append({"dep": f["name"], "dep_id": f["id"], "ctrl": ctrl,
                     "answers": {ctrl: hid, f["name"]: "Yes" if f["cond"].startswith("is") else "42"}})
        if len(scen) >= cap:
            break
    return scen


def as_ids(answers):
    return {str(NAME_TO_ID[k]): v for k, v in answers.items() if k in NAME_TO_ID}


def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    key = post(f"{API}/admin/tenants/polartie/login-key", {}, {"X-Admin-Token": "dev-admin-token-p0"})["api_key"]
    H = {"Authorization": f"Bearer {key}", "X-Project": "smartform"}
    scen = build_scenarios(cap)
    print(f"# built {len(scen)} backward-edit scenarios from the real form", flush=True)

    orig_leaks = sop_leaks = 0
    voided_total = 0
    rec_ms = []
    examples = []
    for s in scen:
        put(f"{MOCK}/api/fill/{UUID}/set-fields", {"__reset__": True}, {"X-Browser-Session-Token": TOK})
        put(f"{MOCK}/api/fill/{UUID}/set-fields", {"values": as_ids(s["answers"])}, {"X-Browser-Session-Token": TOK})

        # Arm ORIG (pt-forms native): nothing clears → read state, count leaks
        gf = get(f"{MOCK}/api/fill/{UUID}/get-fields", {"X-Browser-Session-Token": TOK})
        # map id-keyed values back to names for the oracle
        id2name = {str(v): k for k, v in NAME_TO_ID.items()}
        ans_named = {id2name.get(str(k), str(k)): v for k, v in gf["values"].items()}
        orig = leaks(ans_named)
        orig_leaks += len(orig)

        # Arm SOP: reconcile (live, apply) → voids stale → re-read → count leaks
        t0 = time.perf_counter()
        rec = post(f"{API}/formflow/reconcile", {"source": SOURCE, "apply": True}, H)
        rec_ms.append((time.perf_counter() - t0) * 1000)
        voided_total += rec.get("count", 0)
        gf2 = get(f"{MOCK}/api/fill/{UUID}/get-fields", {"X-Browser-Session-Token": TOK})
        ans2 = {id2name.get(str(k), str(k)): v for k, v in gf2["values"].items()}
        sop = leaks(ans2)
        sop_leaks += len(sop)
        if len(examples) < 4 and orig:
            examples.append({"controlling": s["ctrl"], "hidden_field": s["dep"],
                             "orig_leaked": orig, "sop_leaked": sop, "voided_by_sop": [v["name"] for v in rec.get("voided", [])]})

    def p(x, q): return round(sorted(x)[max(0, int(len(x) * q) - 1)], 1) if x else None
    report = {
        "scenarios": len(scen),
        "ORIG_ptforms_stale_answers_that_leak_to_pdf": orig_leaks,
        "SOP_reconcile_stale_answers_remaining": sop_leaks,
        "fields_voided_by_reconcile": voided_total,
        "reconcile_latency_ms": {"p50": p(rec_ms, .5), "p95": p(rec_ms, .95), "max": round(max(rec_ms), 1) if rec_ms else None},
        "examples": examples,
    }
    print(json.dumps(report, indent=2))
    with open("scripts/smartform_bench/reconcile_ab_results.json", "w") as fh:
        json.dump(report, fh, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
