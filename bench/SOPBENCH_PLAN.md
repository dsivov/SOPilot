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
