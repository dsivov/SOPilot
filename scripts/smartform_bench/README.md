# SmartForm A/B benchmark — original realtime flow vs SOP deterministic flow

**The decision under test:** at each turn of a medical intake form, *"which field is
next?"* — ask the right question, skip the ones the form's rules hide. Ground truth =
the form's own show-if rules (`FieldCondition`: `isYes`/`isNo`/`values[{id}]`),
evaluated exactly.

| Arm | What it does | Who does this today |
|-----|--------------|---------------------|
| **A — baseline** | A realtime model **infers** the next field from the whole form + answers | pt-forms today (weak 32k voice model) |
| **B — SOP** | The deterministic driver **evaluates** the FieldConditions (`constraints.py`); the strong model only **phrases** | this spike (`/formflow/prepare`) |

Arm A is run at two tiers — `A-weak = gpt-4o-mini` (proxy for the weak realtime voice
model) and `A-strong = gpt-4o` (upper bound for model inference) — so the baseline
cannot be dismissed as "crippled": the point is that per-turn inference over 269
interacting conditions is error-prone *even for a strong model*, while deterministic
evaluation is not.

## Results (269-question Injured Worker Questionnaire, 120 decision points)

| Arm | Accuracy (all) | Accuracy (skip/gating decisions) | Dangerous errors¹ | Decision latency p50 / p95 | Decision context / turn |
|-----|:--:|:--:|:--:|:--:|:--:|
| A-weak (gpt-4o-mini) | 83.3% | **55.9%** | 15 (14 asked a hidden field) | 933 ms / 2259 ms | ~4.7k tokens |
| A-strong (gpt-4o) | 86.7% | **67.6%** | 11 (11 asked a hidden field) | 819 ms / 1419 ms | ~4.7k tokens |
| **B (deterministic / SOP)** | **100%** | **100%** | **0** | **~0 ms / 0.3 ms** | **0 tokens** |

¹ *Dangerous error* = surfaced a field the rules HIDE (asked an inapplicable medical
question), or re-asked an answered one. These are the clinically meaningful failures.

**Arm B, live end-to-end** (through the real endpoint, answers read from pt-forms
`get-fields`): deterministic decision incl. the live fetch **p50 18.7 ms / p95 23.9 ms**;
with strong-model phrasing **p50 1068 ms / p95 1741 ms** — the same order as Arm A, but
the model only *phrases a pre-decided, correct question* at the tolerant Next/Prev
boundary, and never decides which field is asked. Its phrasing context is ~550 tokens
(the current stage's playbook), not the whole form.

**Takeaway:** on the skip-logic decisions that matter, even a strong model is wrong
~1 in 3 turns; the deterministic engine is right every time, at ~0 ms and 0 decision
tokens. The strong model is moved off the correctness path to phrasing only.

*Token counts use `tiktoken` if installed, else a chars/4 approximation (labeled in the
output).*

## Head-to-head: backward edit-propagation (the real Original-vs-SOP difference)

On "which field is next?" the original pt-forms flow and the SOP flow are **identical**
(both deterministic → 100%). They diverge only when a patient **corrects an earlier
answer** so a previously-answered question becomes hidden:

- **Original pt-forms** leaves the stale answer in place (`_get_next_empty_field_id`
  skips already-valued fields; the PDF drops only blank/"not applicable"), so it prints.
- **SOPilot + reconcile** (`POST /formflow/reconcile`) recomputes visibility with
  `constraints.py` and voids every answered-but-hidden field (to a fixpoint), writing
  "not applicable" back to pt-forms.

`reconcile_ab.py` builds backward-edit scenarios from the real form and measures stale
answers that survive to output:

| Setup | Stale answers leaking to PDF (60 scenarios) | Reconcile latency | Next-field accuracy |
|---|:--:|:--:|:--:|
| Original pt-forms | **61** | — | 100% |
| SOPilot (SOP + reconcile) | **0** | p50 30 ms / p95 37 ms | 100% |

Honest note: this is not exclusive tech — pt-forms could add the same reconcile. What
it proves is the gap is **real and sized**, and SOPilot's constraint graph closes it.

```bash
backend/.venv/bin/python scripts/smartform_bench/mock_ptforms.py 9700 &   # if not already running
backend/.venv/bin/python scripts/smartform_bench/reconcile_ab.py 60
```

## Reproduce

```bash
# 1. publish the form as a SOP (id-aware __map__ block)
backend/.venv/bin/python scripts/form_to_sop.py \
  --form "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json" \
  --name "Injured Worker Questionnaire" --tenant polartie --project smartform --publish --reset

# 2. (optional, for the live-endpoint numbers) a conformant mock pt-forms get-fields
backend/.venv/bin/python scripts/smartform_bench/mock_ptforms.py 9700 &

# 3. the A/B accuracy + latency + context benchmark (needs OPENAI_API_KEY)
backend/.venv/bin/python scripts/smartform_bench/ab_bench.py 120
```

`mock_ptforms.py` serves the **exact** pt-forms `get-fields`/`set-fields` contract
(id-keyed indexed schema, `{FieldName}`→id-resolved conditions, `X-Browser-Session-Token`
auth) — verified against the Laravel `McpController` source — so the live path exercises
the same seam a real pt-forms server would. The real PolarTie stack (Laravel/MySQL/Redis)
is not stood up in this environment; point `/formflow/prepare` at a real base URL +
submission + token and it works unchanged.
