#!/usr/bin/env python3
"""Live end-to-end check for D-1 + D-9 against a running API (embedded supervisor).

Flow:
  1. bootstrap tenant + three projects (both / sop / retrieval)
  2. create + publish a renewal SOP with a data dependency (mock, 800 ms)
  3. run 3 training sessions (Greeting → VerifyIdentity → PitchRenewal, outcome=success)
     to accumulate precedent traces
  4. new session: after turn 0 the supervisor should prefetch the policy dep
     (predicted at offset 2) — assert the pool fills and turn 2 consumes it speculatively
  5. assert mode gating over HTTP: sop-only (prompt, no context block),
     retrieval-only (context block, no prompt)

Usage: e2e_check.py [base_url]  (default http://127.0.0.1:8100)
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
ADMIN_TOKEN = "dev-admin-token-p0"

FAILURES: list[str] = []


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:300]}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS: " if ok else "FAIL: ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


SOP_DEF = {
    "definition": {
        "name": "renewal-e2e",
        "conversation_profile": {
            "agent_role": "Renewal agent",
            "goal": "Renew the policy",
            "success_markers": ["AgreedToRenew"],
            "failure_markers": ["HardDecline"],
        },
        "agent_actions": [
            {"name": "Greeting"},
            {"name": "VerifyIdentity"},
            {"name": "PitchRenewal", "data_dependencies": ["policy"]},
        ],
        "user_states": [{"name": "AgreedToRenew"}, {"name": "HardDecline"}],
        "data_dependencies": [
            {
                "name": "policy",
                "kind": "mock",
                "expected_latency_ms": 800,
                "cache_ttl_s": 120,
                "config": {"text": "policy #INS-777: premium 480/yr, renewal due Aug 1"},
            }
        ],
        "sop": {
            "edges": [
                {"src": "Greeting", "dst": "VerifyIdentity"},
                {"src": "VerifyIdentity", "dst": "PitchRenewal"},
            ]
        },
    }
}


def auth(key: str, project: str) -> dict:
    return {"Authorization": f"Bearer {key}", "X-Project": project}


def run_training_session(key: str, project: str, sop_id: str) -> None:
    sess = call("POST", "/sessions", {"sop_id": sop_id}, auth(key, project))["session_id"]
    for action, msg in [("Greeting", "hi"), ("VerifyIdentity", "sure, ID 123"), ("PitchRenewal", "ok tell me")]:
        call(
            "POST",
            f"/sessions/{sess}/plan-turn",
            {"user_message": msg, "action": action, "cohort": "Loyal", "mood": "calm"},
            auth(key, project),
        )
    call("POST", f"/sessions/{sess}/outcome", {"outcome": "success"}, auth(key, project))
    call("POST", f"/sessions/{sess}/end", {}, auth(key, project))


def main() -> int:
    slug = f"e2e{int(time.time())}"
    t = call("POST", "/admin/tenants", {"slug": slug}, {"X-Admin-Token": ADMIN_TOKEN})
    key = t.get("api_key", "")
    check("tenant bootstrap", bool(key), str(t))

    for proj, mode in [("pboth", "both"), ("psop", "sop"), ("pret", "retrieval")]:
        r = call("POST", "/admin/projects", {"slug": proj, "subsystems": mode}, auth(key, proj))
        check(f"project {proj} ({mode})", "_status" not in r, str(r))

    sops = {}
    for proj in ("pboth", "psop", "pret"):
        s = call("POST", "/sops", SOP_DEF, auth(key, proj))
        call("POST", f"/sops/{s['id']}/publish", None, auth(key, proj))
        sops[proj] = s["id"]

    # --- training history on the 'both' project (feeds the precedent predictor)
    for _ in range(3):
        run_training_session(key, "pboth", sops["pboth"])
    print("· 3 training sessions recorded")

    # --- fresh session: supervisor should prefetch the policy dep after turn 0
    sess = call("POST", "/sessions", {"sop_id": sops["pboth"]}, auth(key, "pboth"))["session_id"]
    r0 = call(
        "POST",
        f"/sessions/{sess}/plan-turn",
        {"user_message": "hello", "action": "Greeting", "cohort": "Loyal", "mood": "calm"},
        auth(key, "pboth"),
    )
    check("turn0 planned (both)", r0.get("chosen_action") == "Greeting", str(r0)[:200])

    pool_size = 0
    for _ in range(30):  # supervisor + 800ms fetch are async — poll up to ~6s
        time.sleep(0.2)
        snap = call("GET", f"/sessions/{sess}/pool", None, auth(key, "pboth"))
        pool_size = snap.get("size", 0)
        if pool_size > 0:
            break
    check("supervisor prefetched into pool", pool_size > 0, f"pool_size={pool_size}")

    call(
        "POST",
        f"/sessions/{sess}/plan-turn",
        {"user_message": "yes it's me", "action": "VerifyIdentity", "cohort": "Loyal", "mood": "calm"},
        auth(key, "pboth"),
    )
    r2 = call(
        "POST",
        f"/sessions/{sess}/plan-turn",
        {"user_message": "what's my premium for renewal?", "action": "PitchRenewal",
         "cohort": "Loyal", "mood": "calm"},
        auth(key, "pboth"),
    )
    stats = r2.get("consume_stats", {})
    check("turn2 consumed speculative fetch", stats.get("consumed", 0) >= 1, str(stats))
    check("turn2 zero live fallback", stats.get("live", 0) == 0, str(stats))
    check("prompt has stage data", "policy #INS-777" in r2.get("prompt_text", ""), r2.get("prompt_text", "")[:200])
    check(
        "latency hidden recorded",
        stats.get("latency_hidden_ms", 0) >= 500,
        str(stats),
    )

    # --- sop-only project: prompt but never a context block; deps resolved live
    sess_s = call("POST", "/sessions", {"sop_id": sops["psop"]}, auth(key, "psop"))["session_id"]
    for action, msg in [("Greeting", "hi"), ("VerifyIdentity", "me")]:
        call("POST", f"/sessions/{sess_s}/plan-turn", {"user_message": msg, "action": action}, auth(key, "psop"))
    rs = call(
        "POST", f"/sessions/{sess_s}/plan-turn",
        {"user_message": "premium?", "action": "PitchRenewal"}, auth(key, "psop"),
    )
    check("sop-only: prompt present", "ROLE:" in rs.get("prompt_text", ""), str(rs)[:200])
    check("sop-only: no context block", rs.get("context_block", "x") == "", rs.get("context_block", "")[:100])
    check("sop-only: dep resolved live", rs.get("consume_stats", {}).get("live", 0) >= 1, str(rs.get("consume_stats")))
    snap_s = call("GET", f"/sessions/{sess_s}/pool", None, auth(key, "psop"))

    # --- retrieval-only project: context block only, no prompt
    sess_r = call("POST", "/sessions", {"sop_id": sops["pret"]}, auth(key, "pret"))["session_id"]
    call("POST", f"/sessions/{sess_r}/plan-turn", {"user_message": "hi", "action": "Greeting"}, auth(key, "pret"))
    rr = call(
        "POST", f"/sessions/{sess_r}/plan-turn",
        {"user_message": "ok", "action": "VerifyIdentity"}, auth(key, "pret"),
    )
    check("retrieval-only: no prompt", rr.get("prompt_text", "x") == "", rr.get("prompt_text", "")[:100])
    check("retrieval-only: subsystems tag", rr.get("subsystems") == "retrieval", str(rr.get("subsystems")))

    # --- SOP legality still enforced everywhere
    bad = call(
        "POST", f"/sessions/{sess_r}/plan-turn",
        {"user_message": "x", "action": "PitchRenewal2"}, auth(key, "pret"),
    )
    check("unknown action rejected", bad.get("_status") in (404, 422) or bad.get("chosen_action") != "PitchRenewal2",
          str(bad)[:150])

    print("---")
    print(f"RESULT: {'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: {FAILURES}'}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
