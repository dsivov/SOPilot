# SOPilot Onboarding Playbook — from raw recordings to a live agent

This is the repeatable recipe for bringing a new customer, or a new topic for
an existing customer, onto SOPilot. It is the "scale by recipe, not by rewrite"
methodology validated on the AENA / Málaga airport POC, written down as an
operating procedure — the SOPilot team's own SOP.

**Read this first — the shape of the work.** The pipeline has nine stages.
Five are mechanical (scripts / the orchestrator do them); **four are
human-judgment gates** that must not be automated away. The gates are marked
🚦 below. Do not skip a gate to save time — every one of them caught a real
problem during the AENA build.

```
 1 Survey ──▶ 2 Clean ──▶ 3 Intents ──▶ 🚦A PII/consent ──▶ 🚦B Pick topics
                                                                   │
 🚦C Review SOPs ◀── 5 Mine SOPs ◀── 4 Author the domain lexicon ◀─┘
        │
        ▼
 6 Provision ──▶ 7 Knowledge base ──▶ 8 Connectors ──▶ 9 Evaluate ──▶ 🚦D Go / no-go
```

The domain-specific inputs a new customer requires are small and explicit: a
**topic lexicon**, a set of **topic/procedure names**, and a one-line **domain
descriptor**. Everything else is mechanical and config-driven. All work happens
in `use_case/` and the raw conversation data **never leaves the analysis
machine and is never committed** (enforced by `.gitignore`).

---

## Per-customer config

One JSON file drives the mechanical stages. Copy the template and fill it in:

```bash
cp use_case/onboarding/config.example.json use_case/onboarding/<customer>.json
```

Key fields: `tenant_slug`, `project_slug`, `subsystems` (`advisory` for
info-desk / knowledge-delivery work, `both` for compliance-critical),
`base_url`, `sops_dir`, `facts_file`, the `domain` block (descriptor + topics),
the `knowledge` block (managed `corpus` or external `context_graph`), and the
`connectors` list. The worked example is `use_case/onboarding/aena.json`.

The customer's API key is never stored in the config — it is resolved from an
environment variable or from `TENANT_KEYS.local.txt` (gitignored). The
orchestrator writes the key there automatically when it creates a new tenant.

---

## Stage 1 — Survey the corpus  *(mechanical)*

Understand what you were given: size, languages, call durations, and the
garbage classes to clean.

```bash
cd use_case/analysis
python survey.py /path/to/customer_dump      # → survey.json + printed summary
```

**Output to check:** dialogue count, date range, language mix, and the garbage
tallies (empty files, single-sided, diarization echo). If more than ~half the
corpus is garbage, the recording setup needs fixing before proceeding.

## Stage 2 — Clean  *(mechanical)*

Remove empty/trivial files, de-duplicate diarization echo, merge split turns.

```bash
python clean.py /path/to/customer_dump        # → cleaned.jsonl (+ clean_report.json)
```

**Acceptance:** kept-dialogue ratio is reasonable (AENA: 68%), and every kept
dialogue has both speakers and ≥3 real turns.

## Stage 3 — Intent distribution  *(mechanical, needs the lexicon from Gate B)*

`intents.py` labels each cleaned dialogue against a keyword **lexicon**. The
lexicon is domain knowledge — see the domain-lexicon step below. Run it once
with a first-draft lexicon to see the topic distribution, then refine.

```bash
python intents.py                              # → intents.json + printed distribution
```

**Output to check:** the share per topic and the unmatched rate. This is the
input to Gate B (which topics to build).

---

## 🚦 Gate A — PII / consent decision  *(human — BLOCKING)*

**Before any external LLM processes conversation content**, get an explicit
decision on data handling. Questions to answer with the customer:

- Does the corpus contain personal data (names, booking references, payment
  details), or is it public-domain operational information?
- Is LLM-assisted processing of the content approved?

Record the decision. For AENA it was "no critical data — publicly available
information," which cleared LLM mining. If the answer involves PII, insert a
masking pass here before Stage 5. **The raw data stays machine-local and out of
git regardless of the decision.**

## 🚦 Gate B — Pick the topics  *(human)*

From the Stage 3 distribution, choose the topics to build first. Rule of thumb:
cover the smallest set of topics that captures the majority of traffic (AENA:
four topics = 71%). For each chosen topic, add to the config's `domain.topics`:
a `key`, a human `sop_title`, and the `lexicon` keywords that identify it.
Re-run Stage 3 with the finalized lexicon.

---

## Author the domain lexicon  *(human — the main domain input)*

The lexicon in `intents.py`'s `LEXICON` dict (and each topic's `lexicon` in the
config) is the one genuinely domain-specific artifact you write by hand. It is
a few dozen keywords per topic, in every language the desk uses. This is where
domain expertise enters the pipeline; everything downstream is generic.

## Stage 5 — Mine the procedures  *(mechanical: LLM does the work)*

For each topic, `mine_sop.py` analyses the cleanest dialogues and writes the
procedure the human agents demonstrably follow, as a plain-text document, then
`build_facts.py` / `mine_facts_full.py` distil every concrete answer agents
gave into an atomic fact base.

```bash
python mine_sop.py                             # → mined/sop_<topic>.txt per topic
python mine_facts_full.py                      # → facts_full.jsonl (all agent answers)
```

Point these at the config's topics/descriptor when onboarding a new domain
(the AENA versions hardcode the airport topics as the reference implementation).

## 🚦 Gate C — Review the mined procedures  *(human — BLOCKING)*

Someone who knows the desk reads each `mined/sop_<topic>.txt` and confirms the
procedure and the facts are correct before anything is published. LLM mining is
good but not authoritative; proper nouns and locations especially need a human
eye. Fix the documents in place, then continue.

---

## Stage 6 — Provision  *(mechanical)*

Create the tenant and project, ingest every reviewed procedure document through
the product's own text-to-SOP pipeline, and publish (the linter gates publish).

```bash
python use_case/onboarding/onboard.py use_case/onboarding/<customer>.json provision
```

**Acceptance:** every SOP ingests lint-clean and publishes. A lint failure is a
real structural problem in the procedure — fix and re-run.

## Stage 7 — Knowledge base  *(mechanical)*

Load the fact base. `mode: "corpus"` loads it into a managed pgvector corpus;
`mode: "context_graph"` pushes it to a Context Graph workspace (the production
knowledge-graph server) for richer entity/relation retrieval.

```bash
python use_case/onboarding/onboard.py use_case/onboarding/<customer>.json knowledge
```

For Context Graph, wait for its ingestion pipeline to finish (it builds the
entity graph asynchronously) before evaluating.

## Stage 8 — Connectors  *(mechanical)*

Create the retrieval connectors from the config and bind them to each
procedure's answering stages. Swapping the knowledge system later (corpus →
Context Graph → a live API) is a connector edit here, never a procedure rewrite.

```bash
python use_case/onboarding/onboard.py use_case/onboarding/<customer>.json connectors
```

**Verify:** the Studio → Connectors view shows each connector reachable (use
its "Test now" probe), and `onboard.py <config> status` lists the SOPs,
connectors and corpora.

## Stage 9 — Evaluate  *(mechanical)*

Prove the agent against **real** needs before going live. Two harnesses:

- `replay.py` — replays held-out real conversations, judged against what the
  human agent provided (good for finding knowledge gaps).
- `aena_ab.py` — the controlled A/B: plain agent + retrieval vs. + procedures
  vs. full SOPilot, all judged the same way, with response-time measurement.
  This is the methodology that produces the customer-facing scorecard.

```bash
python aena_ab.py 12 A_rag,A_rag_sop,B         # → aena_ab_results.json + printed scorecard
```

## 🚦 Gate D — Go / no-go  *(human)*

Review the evaluation. The bar: the SOPilot configuration should reach the
quality class of the best prompt-based agent (procedures are the value) at
lower latency, with routing/tracking/audit the plain agents lack. If a topic
underperforms, the miss notes point at the cause (usually a knowledge gap that
needs more facts or a live data feed) — iterate Stages 5/7 and re-run Stage 9.
Only then promote to production.

---

## Production notes

- **Run tests in a separate project/workspace.** Every conversation writes a
  precedent trace and the agent learns from them; simulated test traffic must
  not train the production agent. Traces are project-scoped, so a
  `<customer>-staging` project isolates test learning for free.
- **Advisory vs. strict mode** is a per-project setting (`subsystems`): advisory
  for information delivery (fastest, measured best for that use), strict
  (`both`) for compliance-critical procedures.
- **Scale to new topics** by repeating Stages 3–9 for the new topic against the
  same tenant — no rewrite, just more procedures and facts.
- The mechanical orchestrator is idempotent; re-running any stage is safe.
