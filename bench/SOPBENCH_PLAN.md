# SOPBench integration plan — external proof on someone else's verifiers

**Benchmark:** SOPBench (arXiv 2503.08669, github.com/Leezekun/SOPBench) — 7
customer-service domains, 97 services whose SOPs are directed action graphs
(helper-function verification must precede each service call), 903 test cases,
**code-based verifiers** scoring five booleans per trajectory (tool-call
validity, constraint responses, final-database match, correct target-action
decision, dependency-graph order).

**Why it fits us:** their headline finding is that models fail *procedurally*
(skipping verification steps), and a one-line adversarial suffix collapses
compliance in every model tested (up to −70pp). Their related-work section
dismisses "external SOP state tracking" systems as impractical — SOPilot is
exactly such a system, so beating internalized compliance on their own
verifiers is a direct, citable counterpoint.

## Experiment design (two arms, identical scoring)

- **Arm A — model alone (their baseline):** unmodified SOPBench run; the full
  SOP text sits in the system prompt once; the model self-navigates.
- **Arm B — model + SOPilot supervision:** same run, same flags, plus our hook
  in `Swarm.get_chat_completion` (their `swarm/core.py:71` re-reads the system
  prompt every turn — a designed-in seam). Per agent step we:
  1. parse the tool calls made so far from the message history,
  2. compute the SOP position and the **legal next tools** with our
     `SOPGraph.allowed_actions` over the converted TaskDefinition,
  3. append one supervisor message: current stage, the legal tool set, and the
     mandated verification wording for the pending service (our stage-prompt
     discipline, minus the pool — there is no external data to prefetch in
     SOPBench, so this exercises the **`sop` subsystem** exactly as D-9
     defines it).
- **Scoring:** their `run_evaluation.py`, untouched, on both arms.

## Conversion (their SOP → our TaskDefinition)

Their `env/domains/<d>/<d>_assistant.py` holds symbolic dependency trees
(`action_required_dependencies` etc.: nested `single/and/or/chain` tuples over
constraint names) plus NL constraint templates. Mapping:

| SOPBench | SOPilot |
|---|---|
| tool (service or helper function) | `agent_action` |
| "helper H verifies constraint of service S" (via `constraint_links` + `constraint_processes`) | ordering edge `H → S` |
| `and` composition | multiple ordering edges (our AND semantics) |
| `or` composition | flagged in `must_say` guidance (our graph is AND-only; the hook's injected text carries the OR alternatives verbatim) |
| NL constraint template | `must_say`-style verification wording on the service action |
| per-task `directed_action_graph` | authoritative per-task edge set (preferred over static domain mapping when present) |

Honest limitation: our ordering graph is AND-only, so `or`/`chain` compositions
are enforced textually (injected instructions), not structurally. If Arm B
wins anyway, that understates the ceiling.

## Pilot

- **Domain:** University (44 test cases — cheapest full domain; GPT-4o-mini
  baseline there per the paper ≈ 41%: large headroom).
- **Model:** gpt-4o-mini, `--tool_call_mode fc`, temp 0, 1 run/case, no user
  simulator (their default scripted user), then the adversarial arm
  (`--user_model adv`) as a second comparison.
- **Metrics:** their overall pass rate + their `error_statistics` breakdown —
  the prediction to falsify: Arm B specifically reduces `dirgraph_violations`
  and `incorrect_action_calls`.

## Deliverables

1. `bench/sopbench/convert_domain.py` — per-task TaskDefinition from a
   SOPBench task instance (uses `directed_action_graph`), lint-clean.
2. `bench/sopbench/supervisor_hook.py` — the ~40-line patch module, applied by
   running their `run_simulation.py` with `PYTHONPATH` shim + env var
   `SOPILOT_SOPBENCH_HOOK=1` (their repo stays unmodified; the hook
   monkeypatches at import via a `sitecustomize`-style loader).
3. Pilot numbers table (A vs B, standard + adversarial) in this file.

## Pilot results — University domain, gpt-4o-mini, fc mode (2026-07-15)

42 evaluated interactions per arm (2 of 44 excluded by their retry logic),
identical flags, scored by their unmodified run_evaluation.py:

| Metric | Arm A (baseline) | Arm B (+SOPilot) | Δ |
|---|---|---|---|
| **Mean pass rate** | **40.5%** | **57.1%** | **+16.7 pp (+41% rel)** |
| total failures | 25 | 18 | −28% |
| dirgraph violations (skipped/mis-ordered verification) | 13 | **4** | **−69%** |
| incorrect action calls | 22 | 16 | −27% |
| constraint violations | 20 | 15 | −25% |
| tool-call errors | 0 | 0 | — |

Sanity anchor: Arm A's 40.5% matches the paper's published GPT-4o-mini
University number (40.91%) — the harness reproduces their setup.

The pre-registered prediction held: supervision's largest effect is on
procedure-order violations, the benchmark's dominant failure class. Remaining
Arm-B failures skew toward outcome errors (wrong permissibility decision from
correctly-gathered information) — a reasoning limit supervision doesn't claim
to fix.

## Adversarial results — same domain/model, jailbreak user (2026-07-15)

Their adversarial mode appends the jailbreak suffix and runs only tasks whose
service action must be REFUSED (36 evaluated per arm — a different, harder
subset than the standard run; compare within-setting only):

| Metric | Arm A (baseline) | Arm B (+SOPilot) | Δ |
|---|---|---|---|
| **Mean pass rate under attack** | **38.9%** | **50.0%** | **+11.1 pp** |
| dirgraph violations | 10 | **2** | **−80%** |
| incorrect action calls | 22 | 17 | −23% |
| constraint violations | 20 | 16 | −20% |

Per-step supervision is structurally jailbreak-resistant: the procedure
re-asserts itself on every step regardless of what the user appended, so the
attack has no single prompt to defeat. Procedure-order compliance under attack
was near-perfect (2 violations in 36 adversarial tasks).

Next: (1) Bank domain (153 cases) transfer check — running; (2) full 7-domain
run + writeup if transfer holds; (3) stronger-model arms (does supervision
still add on top of GPT-4.1-class baselines?).
