#!/usr/bin/env python3
"""Answer-capture A/B — the robot turning a messy spoken answer into the right value.

  Arm A = Original pt-forms: the WEAK realtime model (gpt-4o-mini) interprets the
          utterance, then pt-forms' rule-based `_validate_and_normalize_answer`
          finalizes it (option-key map, yes/no dictionary, number char-strip). Ported
          faithfully from mcp/forms_mcp_server.py.
  Arm B = SOPilot: POST /formflow/interpret — the STRONG supervisor (gpt-4o) normalizes
          the utterance using the SOP flow block's coercion rules.

Curated test set of realistic messy answers with an unambiguous expected stored value,
across categories (word-numbers, colloquial yes/no, choice paraphrase, dates, don't-know,
multilingual). Reports accuracy per arm (overall + by category) and latency.
"""
import datetime
import json
import re
import statistics as st
import time
import urllib.request

from openai import OpenAI

API = "http://127.0.0.1:8100"
WEAK = "gpt-4o-mini"
TODAY = datetime.date.today().isoformat()
_oai = OpenAI()

# (category, question, ftype, options, patient_said, expected_stored_value)
CASES = [
    ("number", "Your age", "Number", None, "I'm forty-five", "45"),
    ("number", "How many children do you have?", "Number", None, "uh, three kids", "3"),
    ("number", "Years at your current job", "Number", None, "about ten years", "10"),
    ("number", "How many prior surgeries?", "Number", None, "none at all", "0"),
    ("number", "Weight in pounds", "Number", None, "one hundred eighty", "180"),
    ("yesno", "Were you admitted to hospital?", "YesNo", None, "yeah, I was", "Yes"),
    ("yesno", "Do you smoke?", "YesNo", None, "nope, never have", "No"),
    ("yesno", "Are you currently employed?", "YesNo", None, "yes, part time", "Yes"),
    ("yesno", "Do you have an attorney?", "YesNo", None, "claro que sí", "Yes"),
    ("yesno", "Have you had this injury before?", "YesNo", None, "no, first time", "No"),
    ("choice", "Which side is injured?", "Button", ["Left", "Right", "Both"], "both sides hurt", "Both"),
    ("choice", "Marital status", "Button", ["Single", "Married", "Divorced", "Widowed"], "my husband passed away last year", "Widowed"),
    ("choice", "Preferred language", "Button", ["English", "Spanish"], "español por favor", "Spanish"),
    ("choice", "Which hand is dominant?", "Button", ["Left", "Right"], "I'm a lefty", "Left"),
    ("choice", "Do you rent or own your home?", "Button", ["Rent", "Own"], "we're still paying the mortgage", "Own"),
    ("unknown", "Date of your last tetanus shot", "Date", None, "honestly no idea", "unknown"),
    ("unknown", "Your primary doctor's name", "Text", None, "I don't remember", "unknown"),
    ("unknown", "Prior claim number", "Text", None, "I'd rather not say", "unknown"),
    ("date", "Date of injury", "Date", None, "March 3rd, 2024", "2024-03-03"),
    ("date", "Date of birth", "Date", None, "January 5 1990", "1990-01-05"),
    ("date", "When did symptoms start?", "Date", None, "the 15th of December 2023", "2023-12-15"),
    ("multilingual", "Do you take any medications?", "YesNo", None, "sí, dos pastillas al día", "Yes"),
    ("multilingual", "How many times per week do you exercise?", "Number", None, "unas cuatro veces", "4"),
    ("text", "Describe how the injury happened", "Text", None, "I slipped on a wet floor at work", "I slipped on a wet floor at work"),
    # --- harder: world-knowledge, unit conversion, slang, inference ---
    ("hard", "Your height in inches", "Number", None, "five foot ten", "70"),
    ("hard", "How many cigarettes do you smoke a day?", "Number", None, "about a pack", "20"),
    ("hard", "How many alcoholic drinks per week?", "Number", None, "half a dozen or so", "6"),
    ("hard", "Weight in pounds", "Number", None, "a buck fifty", "150"),
    ("hard", "Employment status", "Button", ["Full-time", "Part-time", "Unemployed", "Retired"], "I got laid off last month", "Unemployed"),
    ("hard", "Pain level", "Button", ["Mild", "Moderate", "Severe"], "I can barely walk", "Severe"),
    ("hard", "Date of injury", "Date", None, "the day after Christmas last year", "2025-12-26"),
]


# ---- pt-forms _validate_and_normalize_answer, ported faithfully ----
def _norm(v):
    return re.sub(r"\s+", " ", str(v).strip().lower())


def _resolve_option_key(options, value):
    t = _norm(value)
    if t == "":
        return None
    if isinstance(options, dict):
        for k in options:
            if _norm(k) == t:
                return str(k)
        for k, lab in options.items():
            if _norm(lab) == t:
                return str(k)
        return None
    if isinstance(options, list):
        for it in options:
            if _norm(it) == t:
                return str(it)
    return None


def pt_normalize(value, ftype, options):
    if options:
        n = _norm(value)
        if n in ("not applicable", "n/a", "na"):
            return "not applicable"
        if n in ("unknown", "don't know", "dont know", "do not know", "declined", "decline", "refused"):
            return "unknown"
        key = _resolve_option_key(options, value)
        return key if key is not None else value
    if ftype in ("CheckBox", "Boolean", "YesNo"):
        s = str(value).strip().lower()
        if s in ("yes", "true", "1", "y", "si", "sí", "oui", "ja"):
            return "Yes"
        if s in ("no", "false", "0", "n", "non", "nein"):
            return "No"
        return value
    if ftype in ("Number", "Numeric", "Integer", "Decimal"):
        cleaned = re.sub(r"[^\d.\-]", "", str(value).strip())
        if cleaned:
            try:
                return str(float(cleaned)) if "." in cleaned else str(int(cleaned))
            except ValueError:
                pass
    return value


# ---- Arm A: weak realtime model interprets, like pt-forms' agent ----
def arm_a(case):
    cat, q, ftype, opts, said, _ = case
    opt_txt = f"\nChoices (use EXACTLY one): {', '.join(opts)}" if opts else ""
    sys_p = ("You are the patient's realtime voice agent filling a medical form. Record the patient's answer as the "
             "exact value to store: numbers as digits, dates as YYYY-MM-DD, yes/no questions as Yes/No, a choice as "
             f"exactly one option, \"don't know\"/refusal as unknown. Today is {TODAY} (resolve relative dates against it). "
             "Output ONLY the value.")
    user = f"QUESTION: {q}\nFIELD TYPE: {ftype}{opt_txt}\nPATIENT SAID: \"{said}\"\nValue:"
    t0 = time.perf_counter()
    try:
        r = _oai.chat.completions.create(model=WEAK, temperature=0, max_tokens=40,
                                         messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user}])
        raw = (r.choices[0].message.content or "").strip().strip('".')
    except Exception as e:
        return "ERR:" + type(e).__name__, (time.perf_counter() - t0) * 1000
    return pt_normalize(raw, ftype, opts), (time.perf_counter() - t0) * 1000


# ---- Arm B: SOPilot /formflow/interpret ----
def arm_b(case, H):
    cat, q, ftype, opts, said, _ = case
    body = {"raw_answer": said, "label": q, "ftype": ftype, "options": opts}
    t0 = time.perf_counter()
    r = urllib.request.Request(f"{API}/formflow/interpret", method="POST",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json", **H})
    res = json.loads(urllib.request.urlopen(r, timeout=60).read())
    return res.get("value", ""), (time.perf_counter() - t0) * 1000


def match(pred, expected, cat, opts):
    p, e = str(pred).strip(), str(expected).strip()
    if cat == "hard":   # dispatch the mixed-type hard cases by their expected shape
        if opts:
            cat = "choice"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", e):
            cat = "date"
        elif e.isdigit():
            cat = "number"
    if cat == "number" or (cat == "multilingual" and e.isdigit()):
        pn = re.findall(r"-?\d+", p)
        return bool(pn) and pn[0] == e
    if cat in ("yesno", "multilingual") and e in ("Yes", "No"):
        return p.strip().lower().startswith(e.lower())
    if cat == "choice":
        return _norm(_resolve_option_key(opts, p) or p) == _norm(e)
    if cat == "unknown":
        return p.strip().lower() in ("unknown", "not applicable")
    if cat == "date":
        return e in p
    if cat == "text":
        ew = {w for w in re.findall(r"[a-z]+", e.lower()) if len(w) > 3}
        pw = set(re.findall(r"[a-z]+", p.lower()))
        return len(ew & pw) >= max(1, int(0.7 * len(ew)))
    return _norm(p) == _norm(e)


def pct(n, d): return round(100 * n / d, 1) if d else 0.0


def main():
    key = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{API}/admin/tenants/polartie/login-key", method="POST",
        headers={"X-Admin-Token": "dev-admin-token-p0"}, data=b"{}")).read())["api_key"]
    H = {"Authorization": f"Bearer {key}", "X-Project": "smartform"}

    from collections import Counter, defaultdict
    res = {"A": [], "B": []}
    lat = {"A": [], "B": []}
    bycat = {"A": defaultdict(lambda: [0, 0]), "B": defaultdict(lambda: [0, 0])}
    misses = {"A": [], "B": []}
    for c in CASES:
        cat, q, ftype, opts, said, exp = c
        (va, la), (vb, lb) = arm_a(c), arm_b(c, H)
        lat["A"].append(la); lat["B"].append(lb)
        for arm, val in (("A", va), ("B", vb)):
            ok = match(val, exp, cat, opts)
            res[arm].append(ok); bycat[arm][cat][0] += ok; bycat[arm][cat][1] += 1
            if not ok:
                misses[arm].append({"q": q, "said": said, "expected": exp, "got": val})

    def summ(arm):
        return {"accuracy_pct": pct(sum(res[arm]), len(res[arm])),
                "correct": sum(res[arm]), "n": len(res[arm]),
                "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(bycat[arm].items())},
                "latency_ms_p50": round(st.median(lat[arm]), 0),
                "misses": misses[arm]}
    out = {"cases": len(CASES),
           "A_original_ptforms_weak+rules": summ("A"),
           "B_sopilot_interpret_strong": summ("B")}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    with open("scripts/smartform_bench/interpret_ab_results.json", "w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
