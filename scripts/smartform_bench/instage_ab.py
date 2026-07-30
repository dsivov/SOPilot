#!/usr/bin/env python3
"""Test the ORIGINAL design variant we skipped: LLM navigates WITHIN a stage.

Original idea: constraints handles stage-to-stage; an LLM handles in-stage navigation
(via the playbook). This measures exactly that in-stage decision — "given only the
current stage's fields + answers, which field is next?" — for a weak and a strong model,
against the form's own rules. Deterministic (constraints.py) is the reference = 100%.

Note (the crux): on this form ALL dependency edges are intra-stage, and each stage's
conditions reference only same-stage fields — so the stage catalog is SELF-CONTAINED,
the best possible case for in-stage LLM navigation. If it still errs, the approach is
weaker than fully-deterministic even under ideal conditions.
"""
import asyncio
import json
import os
import re
import statistics as st
import sys
import time

from sopilot.bench.llm import client
from sopilot.constraints import evaluate_condition

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import form_to_sop as f2s  # noqa: E402

FORM = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
WEAK, STRONG = "gpt-4o-mini", "gpt-4o"


def load():
    raw = json.load(open(FORM))
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    by = {}
    for f in raw:
        if f.get("FieldType") == "Section" or "FieldName" not in f:
            continue
        by[f["FieldName"]] = {"name": f["FieldName"], "cond": f.get("FieldCondition") or "",
                              "label": str(f.get("FieldNameAlt") or ""),
                              "opts": list((f.get("FieldOptions") or {}).values())
                              if isinstance(f.get("FieldOptions"), dict) else (f.get("FieldOptions") or [])}
    sections, _ = f2s.parse(FORM)
    stages = f2s.build_stages(sections)
    stage_fields, stage_of = [], {}
    for i, stg in enumerate(stages):
        names = [x["name"] for x in stg["fields"]]
        stage_fields.append(names)
        for n in names:
            stage_of[n] = i
    return by, stage_fields, stage_of


BY, STAGE_FIELDS, STAGE_OF = load()


def visible(name, ans):
    c = BY[name]["cond"]
    return evaluate_condition(c, ans) if c else True


def instage_next(stage_names, ans, cursor):
    """Deterministic reference: next visible unanswered field within this stage after cursor."""
    started = cursor is None
    for n in stage_names:
        if not started:
            if n == cursor:
                started = True
            continue
        if visible(n, ans) and str(ans.get(n, "")).strip() == "":
            return n
    return None  # stage complete


def synth(name, policy):
    o = BY[name]["opts"]
    yn = o and {str(x).lower() for x in o} <= {"yes", "no"}
    if yn:
        return "Yes" if policy == "yes" else "No" if policy == "no" else ("Yes" if len(name) % 2 else "No")
    return "40"


def gen_points(cap):
    pts = []
    for policy in ("yes", "no", "alt"):
        for si, names in enumerate(STAGE_FIELDS):
            ans = {}
            # answer preceding stages fully (not needed for in-stage decision correctness)
            cursor = None
            guard = 0
            while guard < 200:
                guard += 1
                nxt = instage_next(names, ans, cursor)
                if nxt is None:
                    break
                gating = bool(BY[nxt]["cond"]) or (cursor is not None)
                pts.append({"ans": dict(ans), "cursor": cursor, "gt": nxt, "stage": si,
                            "gating": bool(BY[nxt]["cond"]), "policy": policy})
                ans[nxt] = synth(nxt, policy)
                cursor = nxt
    step = max(1, len(pts) // cap)
    return pts[::step][:cap]


def stage_catalog(si):
    lines = ["Fields in THIS stage, in order. id=FieldName | show-if | label. "
             "A field is VISIBLE only if its show-if holds (isYes/isNo/values[{name}]); blank = always visible."]
    for n in STAGE_FIELDS[si]:
        c = BY[n]["cond"] or "-"
        o = f" [choices: {', '.join(map(str, BY[n]['opts']))}]" if BY[n]["opts"] else ""
        lines.append(f"{n} | {c} | {BY[n]['label']}{o}")
    return "\n".join(lines)


async def arm(model, pt):
    cat = stage_catalog(pt["stage"])
    sysp = ("You navigate WITHIN one stage of a form. Given the stage's fields and the answers so far, "
            "return the NEXT field to ask = the first field AFTER the current one, in order, that is VISIBLE and "
            'NOT yet answered. If none remain in this stage, return {"next": "DONE"}. '
            'Reply ONLY JSON: {"next": "<FieldName or DONE>"}.\n\n' + cat)
    user = f'CURRENT FIELD: {pt["cursor"]}\nANSWERS: {json.dumps(pt["ans"])}\nNext field in this stage?'
    t0 = time.perf_counter()
    try:
        r = await client().chat.completions.create(
            model=model, temperature=0, max_tokens=20, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sysp}, {"role": "user", "content": user}])
        ms = (time.perf_counter() - t0) * 1000
        pred = json.loads(r.choices[0].message.content or "{}").get("next")
    except Exception as e:
        return None, (time.perf_counter() - t0) * 1000, "ERR:" + type(e).__name__
    return pred, ms, None


def pct(n, d): return round(100 * n / d, 1) if d else 0.0


async def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    pts = gen_points(cap)
    gating = [p for p in pts if p["gating"]]
    print(f"# in-stage decision points: {len(pts)} ({len(gating)} conditional) across {len(STAGE_FIELDS)} stages", flush=True)
    out = {"decision_points": len(pts), "conditional_points": len(gating), "arms": []}
    for tier, model in (("in-stage LLM · gpt-4o-mini", WEAK), ("in-stage LLM · gpt-4o", STRONG)):
        sem = asyncio.Semaphore(8)
        async def one(p):
            async with sem:
                return await arm(model, p)
        res = await asyncio.gather(*[one(p) for p in pts])
        correct = sum(1 for (pred, _, _), p in zip(res, pts) if pred == p["gt"])
        gc = sum(1 for (pred, _, _), p in zip(res, pts) if p["gating"] and pred == p["gt"])
        lat = [ms for _, ms, _ in res]
        out["arms"].append({"arm": tier, "accuracy_pct": pct(correct, len(pts)),
                            "accuracy_on_conditional_pct": pct(gc, len(gating)),
                            "latency_ms_p50": round(st.median(lat), 0)})
        print(f"  {tier}: {pct(correct,len(pts))}%  (conditional {pct(gc,len(gating))}%)", flush=True)
    out["arms"].append({"arm": "deterministic constraints.py (reference)", "accuracy_pct": 100.0,
                        "accuracy_on_conditional_pct": 100.0, "latency_ms_p50": 0})
    print(json.dumps(out, indent=2))
    with open("scripts/smartform_bench/instage_ab_results.json", "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
