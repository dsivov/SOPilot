# PolarTie SmartForm — problem register

Working register for the PolarTie **SmartForm** engagement (a voice/chat robot that
helps customers fill long, conditional medical forms — e.g. the 271-field QME
"Injured Worker Questionnaire"). Repo learned: `github.com/PolarTie/pt-forms-management`
(cloned at `/storage/Work/pt-forms-management`). Prompt: `mcp/agent_prompt.md`.

**Status:** discovery done · **Navigation chosen as the first problem to solve** ·
integration shape (where SOPilot sits) deliberately deferred until we agree the fixes.
All five groups below are in scope — navigation is sequenced first, not solo.

---

## Architecture facts that shape every problem

- **Server-authoritative state machine + deliberately-minimal voice agent.** The MCP
  server (`mcp/forms_mcp_server.py`, Python/FastMCP) owns navigation, skip-logic,
  validation and state; the LLM agent only relays one question and sends one answer
  at a time (`agent_prompt.md:1-2`, `:308`). This is the same shape SOPilot uses.
- **Stack:** Laravel 12 / MySQL / Redis backend (forms CRUD, submissions, SSE);
  Vue 3 frontend (fill + admin + visual builder); Python FastMCP `mcp/` agent-tool
  server on `/mcp:8001`; `pt_ai_agent_mcp` SDK (the Session/Event supervisor loop —
  **an uninitialised submodule, not in this checkout**).
- **Form = dependency graph.** Fields carry **`FieldCondition`** — a sandboxed boolean
  expr (`isYes({f})`, `values[{f}] > 0`, `and/or/not`) evaluated server-side
  (`_evaluate_field_condition` `:474`). `{FieldName}` tokens are rewritten to
  **sequential numeric ids** at load (`Form::getIndexedSchema` `Form.php:421`) — order
  matters. A **second, fuzzy NL `IF/THEN` rule layer** also skips fields
  (`mcp/rules/*.md`, `_evaluate_form_rules_for_field` `:1127`). Skipped ⇒ stored as
  literal `"not applicable"`.
- **Answers are a flat `FieldName→value` JSON blob** (`submissions.data`), repeater
  entries suffixed `[i]`. No relational fields/answers tables.
- **MCP tools:** `forms_init, forms_get_next_field, forms_send_field_answer,
  forms_send_verification_answer, forms_acknowledge_info, forms_go_to_field,
  forms_navigate, forms_get_first_empty_field, forms_get_progress, forms_submit,
  forms_cancel`.

**Unifying insight:** the five problems are all consequences of one fact — *a form is
a stateful dependency graph too big for a 32k voice model to hold* — and the design
answer (server-authoritative state + minimal per-turn agent) is one SOPilot already
embodies. That's why several problems map cleanly onto SOPilot machinery.

---

## The five problem groups

### 1. Form navigation — **PRIORITY 1 (solve first)**
Move to any field by meaning or position, in any language: "fix my age", "back to my
address", "two back", "next unanswered", "question 5".
- **Today:** `forms_navigate(request, current_field_id)` → server resolves via
  **question-text matching** (`_resolve_field_id_by_question` `:342`); steps over
  skipped fields; returns a field, `Ambiguous:`, `No matching field`, or
  `No question in that direction`.
- **Hard residual:** paraphrase/synonym/**multilingual** matching; **match by answer
  content** ("where I said Ryanair"); **near-duplicate ambiguity** (~20× "hours per day
  you X"; "doctor's name" in 3 sections + repeater); **repeater-instance** targeting
  ("the 2nd object's weight"); relative moves over a **sparse/looping** graph +
  **display-number vs internal-id** confusion; landing only on **eligible** (non-skipped)
  fields; ~1s latency + voice-friendly single-question disambiguation.
- **SOPilot leverage — HIGH (built analog):** the intake/switch **router**
  (semantic intent→graph node, multilingual, ASR-robust, per-turn re-route) is a better
  `forms_navigate`; a **semantic field index** (question + `FieldNameAlt` + current
  value, confidence-scored) improves precision + disambiguation; the **constraint
  engine** defines the eligible target set.
- **Open:** which failure mode actually bites in prod (matcher / near-dup / repeater /
  relative / latency)? Point at live cases on `.../fill/25863473`.

### 2. Question answering
Interpret messy voice/ASR input → the field's validated value.
- **Today:** the LLM agent does this, guided per-field by `Answer format:` + prompt
  rules (date normalisation incl. relative + cross-field "same day as the injury",
  transliteration, granularity, option-key matching, `unknown`/`not applicable`); server
  returns `Validation error` to re-ask; `#alert` sentinel for alert conditions.
- **Hard residual:** interpretation robustness under ASR noise; cross-field derivations;
  **alert recognition** is fragile in a "minimal" agent; long free-text read-back/confirm.
- **SOPilot leverage — MODERATE:** per-turn steering could inject stronger per-field
  validation + **precedent** ("how prior callers answered this field"); advisory mode
  fits the voice latency budget.

### 3. State management — remember + update (change-of-mind)
Persist answers, resume across sessions, and let the user change any earlier answer.
- **Today:** server stores answers (`submissions.data`); resume via
  "CONTEXT — answers already saved" block; edits via `forms_navigate` + re-answer, and a
  `Requested change:` revision walk (agent_prompt rule 7b); continuation-verification
  gate before resuming.
- **Hard residual (the crux, with #4):** **edit propagation.** When a changed answer
  flips a skip condition, already-collected dependent answers become moot (must be
  invalidated?), and previously-skipped fields may become **required** and need
  re-surfacing. Is this recomputed today, or only forward? Cascade correctness on edits.
- **SOPilot leverage — HIGH:** a formal graph makes "given the new state, what's now
  valid / newly-required / contradictory" principled instead of ad-hoc.

### 4. Sync between questions — dependencies/consistency
The field dependency graph: forward visibility + backward propagation + contradiction/
alert detection + derived values.
- **Today:** two **forward** skip engines (deterministic `FieldCondition` AST +
  fuzzy NL `IF/THEN` rules). Both can fire on one field.
- **Hard residual:** **bidirectional** propagation on edits (with #3); contradiction/
  alert detection across answers; **authoring & verifying** a correct dependency graph
  for 271-field forms (the two overlapping skip mechanisms make this error-prone);
  reorder/rename silently changes numeric ids.
- **SOPilot leverage — HIGHEST (this *is* SOPilot):** the constraint-graph engine
  (rules: requires/conflicts/enum) evaluated over state = forward + backward + conflicts,
  **plus authoring/verification tooling** (like the config schema/rules admin).

### 5. Realtime (voice) context size — 32k
- **Sub-problem (a): form too big for the prompt** → **already solved** by
  one-field-at-a-time + server-held state; the agent never sees all 271 fields.
- **Sub-problem (b): a long call's transcript grows past 32k anyway** → **open.** A
  30–60 min, 271-field session accumulates turns until it blows the window.
- **Hard residual:** because the **server is the source of truth**, the voice model's
  history is *disposable* — need aggressive sliding-window/compaction, periodic session
  re-init that reloads only `guidelines + current field + a compact answers-so-far
  summary`, and a lean, stable system prompt.
- **SOPilot leverage — HIGH (thesis match):** "hold state in the engine, keep the
  responder context minimal, assemble only what this turn needs" is precisely the
  mitigation; SOPilot could own per-turn context assembly + the answers-so-far summary +
  compaction triggers.

---

## Leverage summary
| # | Problem | Mostly-handled today | Hard residual | SOPilot leverage |
|---|---|---|---|---|
| 1 | Navigation | server text-match + skip-stepping | precision, repeater/relative, multilingual, latency | **High (router + field index)** |
| 2 | Answer quality | LLM agent + `Answer format:` | robustness, alerts, derivations | Moderate (steering + precedent) |
| 3 | State (remember+edit) | server store + resume | **edit propagation** | High (graph recompute) |
| 4 | Sync/dependencies | 2 forward skip engines | backward propagation, conflicts, authoring | **Highest (is SOPilot)** |
| 5 | 32k context | one-field-at-a-time (a) | long-call compaction (b) | High (minimal-context thesis) |

## Engagement options (how much SOPilot owns) — at least three, all open

These are orthogonal to *which* problem we fix first; they decide *who runs the code*.

### Option A — SOPilot handles all issues ("do it in SOP")
SOPilot becomes the brain behind the robot: it holds the form as a **constraint graph**
and owns navigation (router), state + **edit-propagation** (graph), 32k context (minimal
per-turn assembly) and answer steering. pt-forms keeps the parts SOPilot shouldn't
reinvent — Laravel store, PDF fill, verification, SSE, the visual builder.
- **Pros:** cleanest fit for #1/#3/#4/#5; one engine; reuses SOPilot's router +
  constraint + supervisor + config-authoring; PolarTie gains a reusable platform beyond
  forms.
- **Cons:** biggest lift; must port the form model (FieldCondition, repeaters, verification,
  alerts, ack fields) into SOPilot and interop with a large working system; parity/
  migration risk; PolarTie cedes the core loop.

### Option B — PolarTie takes our ideas ("advisory")
We deliver the designs (field index, edit-propagation algorithm, context-compaction
scheme) and PolarTie implements them **inside their own MCP server**. No SOPilot code
deployed.
- **Pros:** lowest lift/risk for both; PolarTie keeps full control; fastest for
  self-contained fixes (e.g. a semantic field index for navigation); no integration or
  state-sync.
- **Cons:** no SOPilot footprint; ideas may land partially; we don't own the engine;
  harder to reuse across PolarTie's other robots; slower feedback loop.

### Option C — Hybrid (SOPilot + enhance the current solution) — *likely pragmatic path*
SOPilot plugs in **where it has a built advantage**; pt-forms keeps what already works.
E.g. SOPilot as the **navigation router + semantic field index** and/or the
**dependency/edit-propagation engine** the forms server calls into, while pt-forms keeps
the backend, PDF, verification, SSE, builder and the minimal agent. Wire via the
`pt_ai_agent_mcp` supervisor/reserved-tool seam (mirrors pt-multirobot).
- **Pros:** best value/risk ratio — keep the working 80%, insert SOPilot only where it's
  genuinely better (#1 router, #3/#4 graph, #5 context); incremental and testable; both
  sides keep their strengths.
- **Cons:** two systems to integrate — need crisp contracts and a single source of truth
  for answers/visibility, or risk double-state.

### Options × problems (where each option is strong)
| Problem | A (SOPilot all) | B (advisory) | C (hybrid) |
|---|---|---|---|
| 1 Navigation | SOPilot router is the resolver | give them the field-index design | **SOPilot router beside `forms_*`** |
| 2 Answer quality | SOPilot steers every turn | prompt/validation guidance | SOPilot per-field steering, optional |
| 3 State + edit-prop | SOPilot graph owns state | give them the recompute algorithm | **SOPilot graph called on edits** |
| 4 Dependencies | SOPilot graph replaces skip engines | give them a graph model | **SOPilot authors/validates conditions** |
| 5 32k context | SOPilot assembles all context | give them the compaction scheme | SOPilot owns per-turn context block |

**Navigation-first is a clean decider:** the *way* we ship the navigation fix effectively
picks the option — a SOPilot **router service beside `forms_navigate`** (C), a **design +
field-index spec** they build (B), or SOPilot **owning resolution end-to-end** (A). So the
first deliverable doubles as the pilot that tests the engagement model.

## Decisions & open questions
- **Decided:** navigation first; all five in scope; integration shape (own the
  dependency engine / supervisor between agent & forms / advise their server) **deferred
  until the fixes are agreed** ("map first").
- **Open to PolarTie:** which navigation failure mode bites in prod? · does the server
  do **backward** edit-propagation today or only forward? · how does the current voice
  loop manage the 32k window on long forms? · access to the `pt_ai_agent_mcp` submodule
  (the outer LLM/voice loop is not in this checkout).
