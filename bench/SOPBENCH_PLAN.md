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

## Transfer check — Bank domain, same model/flags (2026-07-15)

Bank is the benchmark's largest domain (14 services, 134 released task
instances — the paper's "153 cases" counts pre-filter variants). Identical
harness, gpt-4o-mini, fc mode, their unmodified evaluator:

| Metric | Arm A (baseline) | Arm B (+SOPilot) | Δ |
|---|---|---|---|
| **Mean pass rate** | **36.6%** | **68.7%** | **+32.1 pp (+88% rel)** |
| total failures | 85 | 42 | **−51%** |
| dirgraph violations | 71 | 24 | **−66%** |
| constraint violations | 58 | 30 | −48% |
| database mismatches | 31 | 12 | −61% |
| incorrect action calls | 38 | 30 | −21% |

The effect roughly doubled versus University (+16.7 pp → +32.1 pp), and the
reason is visible in the baseline's failure mix: Bank SOPs are deeper (chained
verification: authentication → ownership → balance …), so the unsupervised
model skips steps far more often (71 dirgraph violations vs 13 in University).
The harder the procedure, the more per-step supervision buys — the scaling
direction the product thesis needs.

Cross-domain summary so far (gpt-4o-mini, their verifiers, zero SOPBench code
modified):

| Setting | Arm A | Arm B | Δ |
|---|---|---|---|
| University, standard (42) | 40.5% | 57.1% | +16.7 pp |
| University, adversarial (36) | 38.9% | 50.0% | +11.1 pp |
| Bank, standard (134) | 36.6% | 68.7% | **+32.1 pp** |

## Full 7-domain sweep — the complete benchmark (2026-07-16)

All 830 released test cases, both arms, identical flags (gpt-4o-mini, fc,
full tool list), their unmodified evaluator:

| Domain | N | Arm A | Arm B | Δ | dirgraph A→B | failures A→B |
|---|---|---|---|---|---|---|
| healthcare | 124 | 17.7% | **58.1%** | **+40.3 pp** | 95→23 | 102→52 |
| bank | 134 | 36.6% | **68.7%** | **+32.1 pp** | 71→24 | 85→42 |
| university | 42 | 40.5% | **57.1%** | **+16.7 pp** | 13→4 | 25→18 |
| library | 66 | 51.5% | 57.6% | +6.1 pp | 16→10 | 32→28 |
| online_market | 172 | 45.4% | 48.3% | +2.9 pp | 78→70 | 94→89 |
| dmv | 97 | 65.0% | 66.0% | +1.0 pp | 14→13 | 34→33 |
| hotel | 195 | 41.0% | 40.5% | −0.5 pp | 72→10 | 115→116 |
| **OVERALL** | **830** | **41.3%** | **54.5%** | **+13.1 pp (+32% rel)** | **359→154 (−57%)** | **487→378 (−22%)** |

Reading the spread honestly:

- **The mechanism is uniform; the payoff isn't.** Supervision cuts
  procedure-order violations in every domain (−57% overall; hotel −86%). It
  converts to pass-rate gains exactly where those violations were the binding
  failure class: healthcare (95 of 102 baseline failures were dirgraph) gains
  +40 pp; bank (71/85) gains +32 pp.
- **Where order wasn't the bottleneck, order fixes don't move pass rate.**
  Hotel: dirgraph 72→10, yet pass flat — its failures are dominated by
  constraint/database booleans (wrong permissibility decisions), which
  supervision doesn't claim to fix. Dmv's baseline was already 65% (easy
  procedures) — little headroom.
- **Deployment implication**: score a domain's failure mix first; supervision
  is the right lever when procedure-skipping dominates — which is exactly the
  failure class the SOPBench paper identifies as the field-wide problem.

## Stronger-model arms — gpt-4o, University + Healthcare (2026-07-16)

Same harness, same flags, assistant model upgraded to gpt-4o in BOTH arms:

| Domain | N | Arm A (gpt-4o alone) | Arm B (+SOPilot) | Δ pass | dirgraph A→B |
|---|---|---|---|---|---|
| university | 42 | 59.5% | 59.5% | 0.0 pp | 6→**0** |
| healthcare | 124 | 74.2% | 77.4% | +3.2 pp | 9→**1** |

Three findings, in order of importance:

1. **Supervision recovers 71–88% of the model-upgrade gap at ~1/17 the model
   cost.** The commercially decisive comparison is supervised-mini vs bare-4o:
   university 57.1% vs 59.5% (supervision closed 16.7 of the 19.0 pp gap —
   88%); healthcare 58.1% vs 74.2% (closed 40.3 of 56.5 pp — 71%). A cheap
   supervised model is most of a frontier upgrade, at a fraction of the
   per-turn price and latency.
2. **The mechanism is model-independent.** Procedure-order violations at
   gpt-4o: 15 → 1 across both domains (−93%). Even frontier-class models
   still skip verification steps under long dialogues; per-step supervision
   removes the failure class essentially entirely (university: literally 0).
   For compliance-gated deployments this matters independently of pass rate —
   "never skipped a mandated check" is a property regulators ask about.
3. **The pass-rate delta shrinks as baselines strengthen** (0 to +3.2 pp),
   because gpt-4o's residual failures are permissibility-reasoning classes
   (constraint decisions, wrong target action) that supervision doesn't claim
   to fix — the same boundary the 4o-mini sweep showed at hotel/dmv. No
   regression in any metric: at worst, supervision is free procedural
   insurance on a strong model.

Not run: Bank/others at gpt-4o (the question is answered; the remaining
domains would refine point 3 at real cost), GPT-4.1-class arms (same design
applies if wanted).

Next: (1) human A/B harness (the in-product Autopilot A/B is the tool);
(2) writeup — the claim set is now complete: full-benchmark +13.1 pp,
adversarial +11.1 pp, supervised-mini ≈ 71–88% of a frontier upgrade,
procedure violations −57% (mini) / −93% (4o).
