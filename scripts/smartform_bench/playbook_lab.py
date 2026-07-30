#!/usr/bin/env python3
"""Playbook lab — take ONE stage and search for the best in-stage-navigation prompt.

Picks the most-conditional stage of the real form, builds a focused set of in-stage
decision points, and runs gpt-4o-mini (the realistic weak model) across several playbook
formats. Reports accuracy (overall + conditional) + latency per format, ranked — so we
can see empirically which playbook content/format navigates best, and whether any beats
the deterministic 100%.
"""
import asyncio
import json
import re
import statistics as st
import sys

from sopilot.bench.llm import client
from sopilot.constraints import evaluate_condition

FORM = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
import os
MODEL = os.environ.get("PLAYBOOK_MODEL", "gpt-4o-mini")   # override to gpt-4o (the real supervisor)
sys.path.insert(0, "/storage/Work/SOPilot/scripts")
import form_to_sop as f2s  # noqa: E402


def load():
    raw = json.load(open(FORM)); raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    by = {}
    for f in raw:
        if f.get("FieldType") == "Section" or "FieldName" not in f:
            continue
        by[f["FieldName"]] = {"name": f["FieldName"], "cond": f.get("FieldCondition") or "",
                              "label": str(f.get("FieldNameAlt") or ""),
                              "opts": list((f.get("FieldOptions") or {}).values())
                              if isinstance(f.get("FieldOptions"), dict) else (f.get("FieldOptions") or [])}
    sections, _ = f2s.parse(FORM)
    stages = [[x["name"] for x in s["fields"]] for s in f2s.build_stages(sections)]
    return by, stages


BY, STAGES = load()


def vis(n, ans):
    return evaluate_condition(BY[n]["cond"], ans) if BY[n]["cond"] else True


def nxt(names, ans, cursor):
    started = cursor is None
    for n in names:
        if not started:
            if n == cursor:
                started = True
            continue
        if vis(n, ans) and str(ans.get(n, "")).strip() == "":
            return n
    return None


_A_YES = re.compile(r"isYes\(\{([^}]+)\}\)")
_A_NO = re.compile(r"isNo\(\{([^}]+)\}\)")
_A_CMP = re.compile(r"values\[\{([^}]+)\}\]\s*(==|!=|>=|<=|>|<)\s*('[^']*'|\"[^\"]*\"|-?\d+)")


def build_candidates():
    """Per field, values that SATISFY and that FALSIFY the atoms referencing it — so a
    walk can make each conditional field visible or hidden (exercises both branches)."""
    sat, neg = {}, {}
    for info in BY.values():
        c = info["cond"]
        if not c:
            continue
        for f in _A_YES.findall(c):
            sat.setdefault(f, []).append("Yes"); neg.setdefault(f, []).append("No")
        for f in _A_NO.findall(c):
            sat.setdefault(f, []).append("No"); neg.setdefault(f, []).append("Yes")
        for f, op, v in _A_CMP.findall(c):
            v2 = v.strip("'\""); num = v2.lstrip("-").isdigit()
            if op == "==":
                s, g = [v2], [str(int(v2) + 1) if num else "other"]
            elif op == "!=":
                s, g = [str(int(v2) + 1) if num else "other"], [v2]
            elif num and op == ">=":
                s, g = [v2], [str(int(v2) - 1)]
            elif num and op == ">":
                s, g = [str(int(v2) + 1)], [v2]
            elif num and op == "<=":
                s, g = [v2], [str(int(v2) + 1)]
            elif num and op == "<":
                s, g = [str(int(v2) - 1)], [v2]
            else:                                     # non-numeric inequality (rare)
                s, g = [v2], ["other"]
            sat.setdefault(f, []).extend(s); neg.setdefault(f, []).extend(g)
    cand = {}
    for f in set(list(sat) + list(neg)):
        vals = list(dict.fromkeys(sat.get(f, []) + neg.get(f, [])))
        if vals:
            cand[f] = vals
    return cand


CAND = build_candidates()


def synth(n, seed):
    if n in CAND:                                    # value drawn from the condition-satisfier
        vals = CAND[n]
        return vals[(hash((n, seed)) % len(vals))]
    o = BY[n]["opts"]; yn = o and {str(x).lower() for x in o} <= {"yes", "no"}
    if yn:
        return "Yes" if (hash((n, seed)) & 1) else "No"
    return "40" if (hash((n, seed)) % 3) else "10"


def pick_stage():
    best, bi = -1, 0
    for i, names in enumerate(STAGES):
        c = sum(1 for n in names if BY[n]["cond"])
        if c > best:
            best, bi = c, i
    return bi


def gen_points(si):
    names = STAGES[si]
    seen, pts = set(), []
    for seed in range(24):                       # diverse answer assignments
        ans, cursor, guard = {}, None, 0
        while guard < 200:
            guard += 1
            g = nxt(names, ans, cursor)
            if g is None:
                break
            key = (cursor, tuple(sorted(ans.items())))
            if key not in seen:
                seen.add(key)
                pts.append({"ans": dict(ans), "cursor": cursor, "gt": g, "cond": bool(BY[g]["cond"])})
            ans[g] = synth(g, seed)
            cursor = g
    return pts


# ---- playbook formats ----
def cat_plain(si, ans):
    out = ["Fields in this stage, in order — name | show-if | label (blank show-if = always visible):"]
    for n in STAGES[si]:
        o = f" [choices: {', '.join(map(str, BY[n]['opts']))}]" if BY[n]["opts"] else ""
        out.append(f"{n} | {BY[n]['cond'] or '-'} | {BY[n]['label']}{o}")
    return "\n".join(out)


def cat_inline(si, ans):
    out = ["Fields in this stage, in order — name | show-if | ANSWER | label:"]
    for n in STAGES[si]:
        a = ans.get(n, "")
        out.append(f"{n} | {BY[n]['cond'] or '-'} | answer={a if str(a).strip() else '∅'} | {BY[n]['label']}")
    return "\n".join(out)


RULES = ("Rules: walk the fields IN ORDER after the current one. Skip a field that already has an answer. "
         "Skip a field whose show-if is present and evaluates FALSE against the answers (isYes/isNo/values[{name}], and/or). "
         "Return the FIRST field that is unanswered AND visible; if none remain, DONE.")

EXAMPLE = ('Example — fields "A | - | .., B | isYes({A}) | .., C | - | .."; answers {"A":"No"}; current A → '
           'B is hidden (isYes(No)=false) → next is C. {"next":"C"}')


def variants(si, ans, cursor):
    u = f'CURRENT FIELD: {cursor}\nANSWERS: {json.dumps(ans)}'
    return {
        "V1 terse-json": (f"Return the next field to ask in this stage. {RULES}\n"
                          'Reply ONLY JSON {"next":"<FieldName or DONE>"}.\n\n' + cat_plain(si, ans),
                          u + "\nNext field?", True, 20),
        "V2 rules-json": (f"You navigate within one stage. {RULES}\n"
                          'Reply ONLY JSON {"next":"<FieldName or DONE>"}.\n\n' + cat_plain(si, ans),
                          u + "\nNext field?", True, 20),
        "V3 inline-values": (f"You navigate within one stage. Each field shows its current ANSWER inline. {RULES}\n"
                             'Reply ONLY JSON {"next":"<FieldName or DONE>"}.\n\n' + cat_inline(si, ans),
                             u + "\nNext field?", True, 20),
        "V4 fewshot": (f"You navigate within one stage. {RULES}\n{EXAMPLE}\n"
                       'Reply ONLY JSON {"next":"<FieldName or DONE>"}.\n\n' + cat_plain(si, ans),
                       u + "\nNext field?", True, 20),
        "V5 cot-brief": (f"You navigate within one stage. {RULES}\nFor each candidate field write one short line "
                         "'name: visible? answered?'. Then end with exactly:  FINAL: <FieldName or DONE>.\n\n" + cat_plain(si, ans),
                         u + "\nWork briefly, then FINAL:", False, 300),
    }


async def run_variant(name, sysp, user, jsonmode, mx, pts_variant):
    sem = asyncio.Semaphore(8)
    async def one(item):
        s, u = item
        async with sem:
            try:
                kw = {"response_format": {"type": "json_object"}} if jsonmode else {}
                r = await client().chat.completions.create(model=MODEL, temperature=0, max_tokens=mx,
                                                           messages=[{"role": "system", "content": s}, {"role": "user", "content": u}], **kw)
                t = r.choices[0].message.content or ""
                if jsonmode:
                    return json.loads(t or "{}").get("next")
                m = re.findall(r"FINAL:\s*([A-Za-z0-9_\-]+)", t)
                return m[-1] if m else None
            except Exception:
                return None
    return await asyncio.gather(*[one(it) for it in pts_variant])


def pct(n, d): return round(100 * n / d, 1) if d else 0.0


async def main():
    si = int(sys.argv[1]) if len(sys.argv) > 1 else pick_stage()
    pts = gen_points(si)
    cond = [p for p in pts if p["cond"]]
    print(f"# stage #{si}: {len(STAGES[si])} fields, {sum(1 for n in STAGES[si] if BY[n]['cond'])} conditional | "
          f"{len(pts)} decision points ({len(cond)} conditional)", flush=True)

    vnames = list(variants(si, {}, None).keys())
    results = {}
    for vn in vnames:
        # build per-point (system,user) for this variant
        items = []
        for p in pts:
            s, u, jm, mx = variants(si, p["ans"], p["cursor"])[vn]
            items.append((s, u))
        preds = await run_variant(vn, None, None, variants(si, {}, None)[vn][2], variants(si, {}, None)[vn][3], items)
        correct = sum(1 for pr, p in zip(preds, pts) if pr == p["gt"])
        cc = sum(1 for pr, p in zip(preds, pts) if p["cond"] and pr == p["gt"])
        results[vn] = (pct(correct, len(pts)), pct(cc, len(cond)))
        print(f"  {vn:22s} overall {results[vn][0]:5}%   conditional {results[vn][1]:5}%", flush=True)

    ranked = sorted(results.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))
    print("\nRANKED by conditional accuracy:")
    for vn, (o, c) in ranked:
        print(f"  {c:5}%  cond · {o:5}% all · {vn}")
    print(f"\n  deterministic constraints.py: 100.0% / 100.0% @ ~0ms")
    with open("scripts/smartform_bench/playbook_lab_results.json", "w") as fh:
        json.dump({"stage": si, "points": len(pts), "conditional": len(cond),
                   "results": {k: {"overall": v[0], "conditional": v[1]} for k, v in results.items()}}, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
