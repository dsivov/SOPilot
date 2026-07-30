#!/usr/bin/env python3
"""Non-navigation judgment tasks in the SmartForm (medical intake) context — weak vs strong.

These are things deterministic gating CANNOT do at all (alerts, contradictions, derived
values, colloquial/implicit medical language). The question: does the strong supervisor
(gpt-4o) beat the weak realtime proxy (gpt-4o-mini) here? Real, medical examples.
"""
import asyncio
import json
import re
import sys

from openai import AsyncOpenAI

_oai = AsyncOpenAI()
WEAK, STRONG = "gpt-4o-mini", "gpt-4o"

# (category, question, context, patient_said, task, expected)
CASES = [
    ("derived-date", "Date you first saw a doctor", "injury date = 2024-03-03", "I saw the doctor the next day",
     "Return the date as YYYY-MM-DD.", "2024-03-04"),
    ("derived-date", "Date of first treatment", "injury date = 2024-03-03", "same day as the injury",
     "Return the date as YYYY-MM-DD.", "2024-03-03"),
    ("former-smoker", "Do you currently smoke?", "", "I quit about five years ago",
     "Return Yes or No.", "No"),
    ("implicit-neg", "Are you currently working?", "", "I was laid off after the injury",
     "Return Yes or No.", "No"),
    ("uncertainty", "Year of your last surgery", "", "sometime in 2019, not sure which month",
     "Return the year.", "2019"),
    ("unit", "Height in inches", "", "five foot ten", "Return a number.", "70"),
    ("unit", "Cigarettes per day", "", "about two packs", "Return a number.", "40"),
    ("bp", "Blood pressure", "", "one twenty over eighty", "Return as systolic/diastolic.", "120/80"),
    ("colloquial-dx", "Do you have diabetes?", "", "my blood sugar runs high and I'm on metformin",
     "Return Yes or No.", "Yes"),
    ("multi-value", "Current medication", "", "I take aspirin and metformin",
     "Return the medications.", "aspirin metformin"),
    ("alert", "Describe your symptoms", "", "chest pain going down my left arm and I'm sweating",
     "Is this a medical red-flag needing an alert? Return ALERT-YES or ALERT-NO.", "ALERT-YES"),
    ("alert", "Describe your symptoms", "", "just a mild runny nose",
     "Is this a medical red-flag needing an alert? Return ALERT-YES or ALERT-NO.", "ALERT-NO"),
    ("alert-critical", "Anything else you want to tell us?", "",
     "honestly I've been having thoughts of ending my life",
     "Is this a critical red-flag needing immediate alert? Return ALERT-YES or ALERT-NO.", "ALERT-YES"),
    ("contradiction", "Describe how the injury happened", "earlier answer: prior surgeries = No",
     "well after my knee surgery back in 2020 it never healed right",
     "Does this CONTRADICT the earlier answer? Return CONTRADICT-YES or CONTRADICT-NO.", "CONTRADICT-YES"),
    ("implicit-injury", "Which body part is affected?", "", "I can't lift my arm above my shoulder anymore",
     "Return the body part.", "shoulder"),
    ("negation-date", "Are you pregnant?", "patient sex = male", "n/a",
     "Return Yes, No, or Not applicable.", "Not applicable"),
]


async def run(model, c):
    cat, q, ctx, said, task, _ = c
    sysp = ("You are the supervisor for a medical intake form — you turn what the patient says into the exact "
            "value/flag to record, using judgment. Output ONLY the required value/flag, nothing else.")
    user = (f"QUESTION: {q}\n" + (f"CONTEXT: {ctx}\n" if ctx else "") +
            f'PATIENT SAID: "{said}"\nTASK: {task}\nOUTPUT:')
    try:
        r = await _oai.chat.completions.create(model=model, temperature=0, max_tokens=30,
                                               messages=[{"role": "system", "content": sysp}, {"role": "user", "content": user}])
        return (r.choices[0].message.content or "").strip().strip('".')
    except Exception as e:
        return "ERR:" + type(e).__name__


def match(pred, c):
    cat, _, _, _, _, exp = c
    p, e = pred.lower(), exp.lower()
    if cat in ("unit", "uncertainty"):
        pn = re.findall(r"\d+", p); return bool(pn) and pn[0] == re.findall(r"\d+", e)[0]
    if cat == "bp":
        return "120" in p and "80" in p
    if cat.startswith("alert") or cat == "contradiction":
        return e.split("-")[-1] in p and ("yes" in p) == ("yes" in e)
    if cat in ("former-smoker", "implicit-neg", "colloquial-dx"):
        return p.startswith(e)
    if cat == "negation-date":
        return "applicable" in p or p.startswith("n/a")
    if cat in ("derived-date",):
        return e in p
    if cat == "multi-value":
        return "aspirin" in p and "metformin" in p
    if cat == "implicit-injury":
        return "shoulder" in p
    return e in p


async def main():
    rows = {}
    for model, tag in ((WEAK, "weak (gpt-4o-mini)"), (STRONG, "strong (gpt-4o)")):
        preds = await asyncio.gather(*[run(model, c) for c in CASES])
        ok = [match(p, c) for p, c in zip(preds, CASES)]
        rows[tag] = (sum(ok), preds, ok)
    n = len(CASES)
    print(f"# {n} SmartForm judgment cases (things deterministic gating cannot do)\n")
    print(f"{'case':16} {'expected':16} {'weak':22} {'strong':22}")
    wp, sp = rows["weak (gpt-4o-mini)"][1], rows["strong (gpt-4o)"][1]
    wok, sok = rows["weak (gpt-4o-mini)"][2], rows["strong (gpt-4o)"][2]
    diffs = []
    for i, c in enumerate(CASES):
        mark = lambda ok: "✓" if ok else "✗"
        print(f"{c[0]:16} {c[5][:15]:16} {mark(wok[i])+' '+wp[i][:20]:22} {mark(sok[i])+' '+sp[i][:20]:22}")
        if sok[i] and not wok[i]:
            diffs.append(c[0])
    print(f"\nWEAK  {rows['weak (gpt-4o-mini)'][0]}/{n}   STRONG {rows['strong (gpt-4o)'][0]}/{n}")
    print(f"strong-wins-where-weak-fails: {diffs}")


if __name__ == "__main__":
    asyncio.run(main())
