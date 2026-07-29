#!/usr/bin/env python3
"""SmartForm A/B benchmark — original realtime-model flow vs SOP deterministic flow.

THE DECISION UNDER TEST: at each turn, "which field is next?" — the safety-critical
choice (ask the right question, skip the hidden ones). Ground truth = the form's own
show-if rules, evaluated exactly.

  Arm A  (baseline / what pt-forms does today): a realtime model INFERS the next
         field from the whole form + answers. Run at two tiers:
           A-weak   = gpt-4o-mini  (proxy for the weak 32k realtime voice model)
           A-strong = gpt-4o       (upper bound for model inference)
  Arm B  (SOP / ours): the deterministic driver EVALUATES the FieldConditions
         (constraints.py). Correct by construction; the strong model only PHRASES.

Reports, per arm: accuracy vs ground truth, dangerous errors (asked a HIDDEN field /
SKIPPED a visible one), per-turn decision latency, and per-turn context size.
"""
import asyncio
import json
import re
import statistics as st
import time

from sopilot.bench.llm import client
from sopilot.constraints import evaluate_condition

FORM = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
TOKEN = re.compile(r"\{([^}]+)\}")
WEAK, STRONG = "gpt-4o-mini", "gpt-4o"

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def toks(s): return len(_enc.encode(s))
    TOK_KIND = "tiktoken/cl100k"
except Exception:
    def toks(s): return (len(s) + 3) // 4
    TOK_KIND = "approx chars/4"


# ---- load form as an id-space field list (exactly get-fields' addressing) ----
def load_fields():
    raw = json.load(open(FORM))
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    name_to_id, fid = {}, 0
    for f in raw:
        if "FieldName" in f:
            fid += 1
            name_to_id[f["FieldName"]] = fid
    fields = []
    for f in raw:
        if f.get("FieldType") == "Section" or "FieldName" not in f:
            continue
        cond = TOKEN.sub(lambda m: "{%s}" % name_to_id.get(m.group(1), m.group(1)),
                         f.get("FieldCondition") or "")
        fields.append({"id": name_to_id[f["FieldName"]], "label": str(f.get("FieldNameAlt") or ""),
                       "type": f.get("FieldType", "Text"), "cond": cond,
                       "opts": list((f.get("FieldOptions") or {}).values())
                       if isinstance(f.get("FieldOptions"), dict) else (f.get("FieldOptions") or [])})
    return fields


FIELDS = load_fields()
BY_ID = {f["id"]: f for f in FIELDS}
ORDER = [f["id"] for f in FIELDS]

# Arm B's strong-model context = the flow block + the CURRENT stage's playbook only
# (bounded), vs Arm A feeding the whole form every turn. Compute from the real pipeline.
import sys as _sys
_sys.path.insert(0, "/storage/Work/SOPilot/scripts")
import form_to_sop as _f2s
_sections, _ = _f2s.parse(FORM)
_stages = _f2s.build_stages(_sections)
_FLOW_T = None
_ID2PLAYTOK = {}
def _init_b_ctx():
    global _FLOW_T
    _FLOW_T = _f2s.flow_text("Injured Worker Questionnaire")
    for s in _stages:
        pt = _f2s.playbook(s)
        for f in s["fields"]:
            _ID2PLAYTOK[f["id"]] = pt
_init_b_ctx()
def b_phrase_ctx_tokens(gt_id):
    pb = _ID2PLAYTOK.get(gt_id, "")
    return toks(_FLOW_T) + toks(pb)


def visible(f, ans):
    return evaluate_condition(f["cond"], ans) if f["cond"] else True


def gt_next(ans, cursor):
    """Ground truth: first field AFTER cursor, in order, visible + unanswered."""
    started = cursor is None
    for fid in ORDER:
        if not started:
            if fid == cursor:
                started = True
            continue
        f = BY_ID[fid]
        if visible(f, ans) and (str(ans.get(fid, "")).strip() == ""):
            return fid
    return None


def synth(fid, policy):
    f = BY_ID[fid]
    lab = f["label"].lower()
    yesno = f["opts"] and {str(o).lower() for o in f["opts"]} <= {"yes", "no"}
    if yesno or any(w in lab for w in ("did you", "have you", "were you", "are you", "do you")):
        return {"yes": "Yes", "no": "No", "alt": "Yes" if fid % 2 == 0 else "No"}[policy]
    if any(w in lab for w in ("age", "how many", "number", "years")):
        return {"yes": "45", "no": "0", "alt": "45" if fid % 2 else "3"}[policy]
    if "date" in lab:
        return "2024-01-15"
    return "N/A"


def gen_decision_points():
    """Walk the form under several answer policies → realistic (answers, cursor, gt) points."""
    pts = []
    for policy in ("yes", "no", "alt"):
        ans, cursor, guard = {}, None, 0
        while guard < 400:
            guard += 1
            nxt = gt_next(ans, cursor)
            if nxt is None:
                break
            f = BY_ID[nxt]
            # a point is "gating" if this field is conditional OR the next author field was skipped
            gating = bool(f["cond"]) or (cursor is not None and nxt != next(
                (i for i in ORDER if i > cursor), None))
            pts.append({"ans": dict(ans), "cursor": cursor, "gt": nxt, "gating": gating, "policy": policy})
            ans[nxt] = synth(nxt, policy)
            cursor = nxt
    return pts


# ---- Arm A: realtime model infers the next field from the whole form ----
def form_catalog():
    lines = ["The form, in order. Each: id | show-if | label. A field is VISIBLE only if its "
             "show-if holds given current answers (isYes/isNo/values[{id}]); no show-if = always visible."]
    for f in FIELDS:
        c = f["cond"] or "-"
        o = f" [choices: {', '.join(map(str, f['opts']))}]" if f["opts"] else ""
        lines.append(f"{f['id']} | {c} | {f['label']}{o}")
    return "\n".join(lines)


CATALOG = form_catalog()
SYS_A = (
    "You are the realtime assistant driving a medical intake form. You decide which question to ask next.\n\n"
    + CATALOG +
    "\n\nRULE: the NEXT field = the first field AFTER the current one, in the listed order, that is VISIBLE "
    "(show-if holds) and NOT yet answered. Skip hidden fields entirely. "
    'Reply with ONLY compact JSON: {"next_id": <int or null>}.')


async def arm_a(model, pt):
    ans_by_id = {str(k): v for k, v in pt["ans"].items()}
    user = (f'CURRENT FIELD (just answered): {pt["cursor"]}\n'
            f'ANSWERS SO FAR (by id): {json.dumps(ans_by_id)}\n'
            "Return the next field id to ask.")
    ctx = toks(SYS_A) + toks(user)
    t0 = time.perf_counter()
    try:
        res = await client().chat.completions.create(
            model=model, messages=[{"role": "system", "content": SYS_A}, {"role": "user", "content": user}],
            temperature=0, max_tokens=30, response_format={"type": "json_object"})
        ms = (time.perf_counter() - t0) * 1000
        pred = json.loads(res.choices[0].message.content or "{}").get("next_id")
        pred = int(pred) if pred is not None else None
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        pred = ("ERR:" + type(e).__name__)
    return pred, ms, ctx


# ---- Arm B: deterministic evaluation (what /formflow/prepare does) ----
def arm_b(pt):
    reps = 200
    t0 = time.perf_counter()
    for _ in range(reps):
        pred = gt_next(pt["ans"], pt["cursor"])
    ms = (time.perf_counter() - t0) * 1000 / reps
    # context the strong model gets to PHRASE = current stage playbook only (bounded).
    # Proxy: the visible fields of the field's local group. We approximate with the
    # per-stage playbook size measured live via the endpoint elsewhere; here we bound it
    # by the single field's own text (what MUST be phrased).
    return pred, ms


def classify(pred, pt):
    """Correct / dangerous-error taxonomy."""
    gt = pt["gt"]
    if isinstance(pred, str) and pred.startswith("ERR"):
        return "error"
    if pred == gt:
        return "correct"
    # asked a hidden (invisible) field?
    if pred is not None and pred in BY_ID and not visible(BY_ID[pred], pt["ans"]):
        return "asked_hidden"
    # picked a field that's already answered, or skipped past the required next one
    if pred is not None and pred in BY_ID and str(pt["ans"].get(pred, "")).strip() != "":
        return "asked_answered"
    return "wrong_field"   # skipped the correct next visible field for another visible one


def pct(n, d): return round(100 * n / d, 1) if d else 0.0


def summarize(name, results, lat, ctx):
    n = len(results)
    from collections import Counter
    c = Counter(results)
    correct = c["correct"]
    danger = c["asked_hidden"] + c["asked_answered"]
    lat = [x for x in lat if x is not None]
    line = {
        "arm": name, "n": n,
        "accuracy_pct": pct(correct, n),
        "wrong": n - correct - c["error"],
        "dangerous_errors": danger,
        "asked_hidden": c["asked_hidden"], "wrong_field": c["wrong_field"],
        "model_errors": c["error"],
        "latency_ms_p50": round(st.median(lat), 2) if lat else None,
        "latency_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 2) if lat else None,
        "latency_ms_max": round(max(lat), 2) if lat else None,
        "context_tokens_median": int(st.median(ctx)) if ctx else None,
        "context_tokens_max": int(max(ctx)) if ctx else None,
    }
    return line


async def main():
    import sys
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    allpts = gen_decision_points()
    # even sample to `cap`, keeping the gating/non-gating mix
    step = max(1, len(allpts) // cap)
    pts = allpts[::step][:cap]
    gating_pts = [p for p in pts if p["gating"]]
    print(f"# form: {len(FIELDS)} fields | decision points generated: {len(allpts)} | "
          f"sampled: {len(pts)} ({len(gating_pts)} gating/skip boundaries) | tokens: {TOK_KIND}", flush=True)

    rows = {}
    # Arm A tiers (concurrency-limited)
    for tier, model in (("A-weak (gpt-4o-mini)", WEAK), ("A-strong (gpt-4o)", STRONG)):
        sem = asyncio.Semaphore(8)
        async def one(p):
            async with sem:
                return await arm_a(model, p)
        out = await asyncio.gather(*[one(p) for p in pts])
        res = [classify(pred, p) for (pred, _, _), p in zip(out, pts)]
        rows[tier] = {"res": res, "lat": [ms for _, ms, _ in out], "ctx": [c for _, _, c in out],
                      "gate_res": [classify(out[i][0], pts[i]) for i in range(len(pts)) if pts[i]["gating"]]}
        print(f"  {tier}: done", flush=True)

    # Arm B deterministic
    b = [arm_b(p) for p in pts]
    b_res = [classify(pred, p) for (pred, _), p in zip(b, pts)]
    b_ctx = []  # measured live via endpoint (see companion run); leave to per-turn playbook

    print("\n=== A/B RESULTS (accuracy = agreement with the form's own show-if rules) ===")
    summaries = []
    for tier in rows:
        r = rows[tier]
        s = summarize(tier, r["res"], r["lat"], r["ctx"])
        s["accuracy_on_gating_pct"] = pct(r["gate_res"].count("correct"), len(r["gate_res"]))
        summaries.append(s)
    sb = summarize("B (deterministic / SOP)", b_res, [ms for _, ms in b], [])
    sb["accuracy_on_gating_pct"] = pct(
        sum(1 for p, (pred, _) in zip(pts, b) if p["gating"] and pred == p["gt"]),
        len(gating_pts))
    # B decides with 0 model tokens; the strong model is used only to PHRASE, with the
    # current stage's playbook (bounded) — report that separately from the decision.
    b_phr = [b_phrase_ctx_tokens(p["gt"]) for p in pts]
    sb["decision_context_tokens"] = 0
    sb["phrasing_context_tokens_median"] = int(st.median(b_phr))
    sb["phrasing_context_tokens_max"] = int(max(b_phr))
    summaries.append(sb)

    print(json.dumps({"decision_points": len(pts), "gating_points": len(gating_pts),
                      "token_method": TOK_KIND, "arms": summaries}, indent=2))
    with open("scripts/smartform_bench/ab_results.json", "w") as fh:
        json.dump({"arms": summaries, "decision_points": len(pts), "gating_points": len(gating_pts)}, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
