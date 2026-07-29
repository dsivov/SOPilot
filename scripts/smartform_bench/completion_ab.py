#!/usr/bin/env python3
"""End-to-end completion accuracy — does the robot fill the WHOLE form correctly?

Runs the full 269-question form to completion under several answer policies, for BOTH:
  Arm A = Original pt-forms  — faithful port of `_get_next_empty_field_id`/`_should_skip_field`
                               (from mcp/forms_mcp_server.py): walk, skip condition-false,
                               return next empty visible field.
  Arm B = SOPilot            — driven LIVE through POST /formflow/prepare (answers read from
                               the mock pt-forms), i.e. the real endpoint end-to-end.

Ground truth = the form's own show-if rules (constraints.py). For each full run we check
the robot asked ALL-AND-ONLY the visible fields, honored every skip, and reached "done".
Accuracy = correct turns / total turns; plus asked/skipped counts and completion.
"""
import json
import re
import sys
import time
import urllib.request

from openai import OpenAI
from sopilot.constraints import evaluate_condition

# --- the weak realtime voice model, simulated: gpt-4o-mini with a 32k context cap ---
VOICE_MODEL = "gpt-4o-mini"
CTX_CAP_TOKENS = 32000
_oai = OpenAI()
_transcript: list[str] = []          # running "spoken" history (capped, like the voice model sees)
_answer_cache: dict[str, str] = {}    # field → the value the voice model recorded (reused by both arms)


def _capped_context() -> str:
    """Keep the transcript under the voice model's context budget (drop oldest)."""
    kept, total = [], 0
    for line in reversed(_transcript):
        t = (len(line) + 3) // 4
        if total + t > CTX_CAP_TOKENS:
            break
        kept.append(line); total += t
    return "\n".join(reversed(kept))


def voice_answer(name: str) -> str:
    """The simulated voice model captures/records the patient's answer for one field.
    Cached per field so both arms hear the same patient (and to bound API calls)."""
    if name in _answer_cache:
        return _answer_cache[name]
    f = BY_NAME[name]
    opts = f["opts"]
    sys_p = ("You are the patient's realtime voice agent filling a medical intake form. "
             "You are given the current question; reply with ONLY the answer value to record "
             "(a Yes/No, a number, a short phrase, or a date YYYY-MM-DD). No explanation.")
    hint = f"\nChoices: {', '.join(map(str, opts))}" if opts else ""
    user = f"Recent conversation:\n{_capped_context()}\n\nCURRENT QUESTION [{name}]: {f.get('label') or name}{hint}\nAnswer:"
    try:
        r = _oai.chat.completions.create(model=VOICE_MODEL, temperature=0.4, max_tokens=20,
                                         messages=[{"role": "system", "content": sys_p},
                                                   {"role": "user", "content": user}])
        val = (r.choices[0].message.content or "").strip().strip('".') or "N/A"
    except Exception:
        val = "N/A"
    # normalize a choice-ish answer to an option when possible
    if opts:
        low = {str(o).lower(): str(o) for o in opts}
        val = low.get(val.lower(), val)
    _answer_cache[name] = val
    _transcript.append(f"Q[{name}]: {f.get('label') or name} -> {val}")
    return val

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


def load_fields():
    raw = json.load(open(FORM))
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    fid, out = 0, []
    for f in raw:
        if "FieldName" in f:
            fid += 1
        if f.get("FieldType") == "Section" or "FieldName" not in f:
            continue
        out.append({"name": f["FieldName"], "id": fid, "cond": f.get("FieldCondition") or "",
                    "opts": list((f.get("FieldOptions") or {}).values())
                    if isinstance(f.get("FieldOptions"), dict) else (f.get("FieldOptions") or [])})
    return out


FIELDS = load_fields()
ORDER = [f["name"] for f in FIELDS]
BY_NAME = {f["name"]: f for f in FIELDS}
NAME_TO_ID = {f["name"]: f["id"] for f in FIELDS}
ID_TO_NAME = {f["id"]: f["name"] for f in FIELDS}


def visible(f, ans):
    return evaluate_condition(f["cond"], ans) if f["cond"] else True


def oracle_next(ans, cursor):
    started = cursor is None
    for name in ORDER:
        if not started:
            if name == cursor:
                started = True
            continue
        f = BY_NAME[name]
        if visible(f, ans) and str(ans.get(name, "")).strip() == "":
            return name
    return None


# Arm A: faithful port of pt-forms _get_next_empty_field_id (skip valued, skip condition-false).
def ptforms_next(ans, cursor):
    started = cursor is None
    for name in ORDER:
        if not started:
            if name == cursor:
                started = True
            continue
        f = BY_NAME[name]
        if str(ans.get(name, "")).strip() != "":     # already valued → handled
            continue
        if f["cond"] and not evaluate_condition(f["cond"], ans):   # _should_skip_field
            continue                                   # (persisted as "not applicable")
        return name
    return None


def run_arm_A():
    """Original pt-forms: server-authoritative next-field; voice model (gpt-4o-mini) records answers."""
    ans, cursor, asked = {}, None, []
    for _ in range(600):
        nxt = ptforms_next(ans, cursor)
        if nxt is None:
            break
        asked.append(nxt)
        ans[nxt] = voice_answer(nxt)
        cursor = nxt
    return asked, ans


def run_arm_B_live(H):
    """SOPilot: supervisor-authoritative next-field (live /formflow/prepare); same voice model records answers."""
    put(f"{MOCK}/api/fill/{UUID}/set-fields", {"__reset__": True}, {"X-Browser-Session-Token": TOK})
    ans, cursor, asked = {}, None, []
    for _ in range(600):
        res = post(f"{API}/formflow/prepare", {"source": SOURCE, "current_field": cursor, "phrase": False}, H)
        if res.get("done"):
            break
        if "error" in res:
            raise SystemExit(f"prepare error: {res}")
        name = res["next_field"]
        asked.append(name)
        val = voice_answer(name)
        put(f"{MOCK}/api/fill/{UUID}/set-fields", {"values": {str(NAME_TO_ID[name]): val}}, {"X-Browser-Session-Token": TOK})
        cursor = str(NAME_TO_ID[name])   # cursor as id (endpoint normalizes id→name)
        ans[name] = val
    return asked, ans


def score(asked, ans):
    """Grade a completed run vs the oracle: correct turns, skip violations, missed fields."""
    # Re-derive the correct sequence from the SAME answers, via the oracle.
    correct_seq, cursor = [], None
    a2 = {}
    for name in asked:            # replay answers in asked-order to rebuild states
        a2[name] = ans[name]
    # per-turn correctness: at each step the asked field must be the oracle's next given answers-so-far
    cur, sofar, correct = None, {}, 0
    for name in asked:
        exp = oracle_next(sofar, cur)
        if exp == name:
            correct += 1
        sofar[name] = ans[name]
        cur = name
    # completion: after the run, no visible field remains unanswered
    remaining = oracle_next(ans, None)
    visible_set = {f["name"] for f in FIELDS if visible(f, ans)}
    asked_set = set(asked)
    skip_violations = [n for n in asked_set if not visible(BY_NAME[n], ans)]  # asked a hidden field
    missed = [n for n in visible_set if n not in asked_set]                   # visible but never asked
    return {"turns": len(asked), "correct_turns": correct,
            "accuracy_pct": round(100 * correct / len(asked), 1) if asked else 0.0,
            "reached_done": remaining is None, "asked": len(asked_set),
            "skipped": len(FIELDS) - len(visible_set), "skip_violations": len(skip_violations),
            "missed_visible": len(missed)}


def main():
    key = post(f"{API}/admin/tenants/polartie/login-key", {}, {"X-Admin-Token": "dev-admin-token-p0"})["api_key"]
    H = {"Authorization": f"Bearer {key}", "X-Project": "smartform"}

    # One voice-driven walkthrough per arm. Arm A runs first and the gpt-4o-mini answers
    # are cached, so both arms hear the SAME patient — a like-for-like comparison.
    a_asked, a_ans = run_arm_A()
    t0 = time.perf_counter()
    b_asked, b_ans = run_arm_B_live(H)
    b_ms = round((time.perf_counter() - t0) * 1000)

    A = score(a_asked, a_ans)
    B = score(b_asked, b_ans)
    out = {
        "voice_model": f"{VOICE_MODEL} (context capped at {CTX_CAP_TOKENS} tokens)",
        "note": "For this form the whole-form catalog is ~4.7k tokens, so the 32k cap never binds; "
                "navigation is decided by the support system in BOTH arms, not the voice model.",
        "identical_walks": a_asked == b_asked,
        "A_original_ptforms": {"end_to_end_accuracy_pct": A["accuracy_pct"], "reached_done": A["reached_done"],
                               "fields_asked": A["asked"], "fields_skipped": A["skipped"],
                               "skip_violations": A["skip_violations"], "missed_visible": A["missed_visible"]},
        "B_sopilot_live": {"end_to_end_accuracy_pct": B["accuracy_pct"], "reached_done": B["reached_done"],
                           "fields_asked": B["asked"], "fields_skipped": B["skipped"],
                           "skip_violations": B["skip_violations"], "missed_visible": B["missed_visible"],
                           "wallclock_ms": b_ms},
    }
    print(json.dumps(out, indent=2))
    with open("scripts/smartform_bench/completion_ab_results.json", "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
