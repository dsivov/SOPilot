# SOPilot — Development Roadmap & Backlog

The consolidated "what's next" for SOPilot, replacing the next-steps scattered
across the AENA report, the architecture doc, and code comments. Grouped by
priority tier. Status as of the P0–P3 + first-customer milestone.

**Where we are:** P0–P3 complete and verified; SOPBench external proof done
(+13–32 pp); first real customer (AENA / Málaga airport) validated end-to-end
on production infrastructure and packaged for handover. The platform is
shippable; this backlog is about turning a validated POC into a broadly
deployable product.

**AENA handover status:** the customer package (manifest: `use_case/delivery/MANIFEST.md`)
now carries **four procedures** (lost luggage, flight/boarding, transport/wayfinding,
airport services & facilities) with **approved-wording prompt blocks bound across
every stage** (7 blocks, governance-versioned), the 111-fact knowledge base, and a
one-command `onboard.py all` stand-up — verified on a fresh tenant, secret-scan clean.
Prompt-block impact measured (advisory, 48 held-out scenarios, on vs off): coverage/
concreteness flat, satisfaction 4.15→4.27, +120 ms p50 — value is consistency/
governance, not coverage. Remaining Tier-1 items are customer-gated (data feeds,
pilot slot).

Legend: 🔴 blocks a customer go-live · 🟠 needed before broad GA · 🟢 opportunistic / research · ⚙️ platform-ops maturity

---

## Tier 1 — Land the AENA production deployment

The POC's own measurements point at these as the highest-value next moves.

| # | Item | Notes | Tag |
|---|---|---|---|
| 1 | **Live counter/flight-status feed integration** | The measured #1 remaining quality lever — dynamic data (today's counter for this flight) that no static store can hold. The `flight-status` connector slot exists (mock); needs AENA's feed + a real fetcher/adapter. | 🔴 |
| 2 | **Canonical terminal/floor data from AENA** | Desk/office locations drift over months; authoritative floor data replaces recollection. Loads as facts or a second connector. | 🔴 |
| 3 | **Voice pilot on the lost-luggage procedure** | Voice channel is built + tested; this is the flagship demo (most emotional call type). Needs a pilot slot + a scripted run-through. | 🔴 |
| 4 | **Expand beyond the top-4 topics — the ranked backlog** | Discovery done (item 4a). Ranked by real volume, each built via the onboarding recipe (Gates A–D): **(1) Airport services & facilities** ~290 conv / ~7%** ✅ built** (mined, published, verified in `aena/malaga`; the 4th procedure, shipped in the handover package); **(2) Language / translation assistance** ~143 / ~3.4% (overlaps item 14a); **(3) Special assistance / reduced-mobility (PMR)** ~87 / ~2% — distinct high-empathy procedure the lexicon missed entirely; **(4) Security, check-in & documents** ~78 clustered + 159 lexicon / ~4%; **(5) Lost & found personal items** ~150 / ~3.5% (wallets/phones/docs — the lost-property desk, distinct from airline baggage). | 🟠 |
| 4a | **Data-driven next-topic discovery** *(EDA lead)* ✅ done | `use_case/analysis/discover_topics.py`: clustered the 1,252 conversations (~29%) not covered by the three current procedures, within-language, and LLM-labelled each cluster (topic vs noise). ~52% of the pool is actionable demand (the rest greeting/closing/chit-chat/unintelligible — itself a finding). Produced the ranked backlog now in item 4; written up in the dataset analysis (`docs/AENA_DATASET_ANALYSIS.html` §Next candidate topics). | ✅ |
| 5 | **Human A/B validation** | Deferred — needs testers. The in-product Autopilot A/B is the harness; run real evaluators against advisory-mode SOPilot once staffed. | 🟢 |

## Tier 2 — Product hardening before broad GA

Real defects and gaps found in the code review, kept out of the AENA path
because they don't affect its configuration, but they must close before other
customers with different setups.

| # | Item | Where | Tag |
|---|---|---|---|
| 6 | **Staleness-gate embedding asymmetry** | `prefetch.py` consume(): compares the raw-utterance embedding against the item's *rendered-query* embedding — for descriptive query templates this can false-flag a fresh item as stale and re-fetch live (latency, never wrong answers). Fix: embed the rendered current query for the comparison, or store both. AENA unaffected (neutral `{user_text}` template). | 🟠 |
| 7 | **Cross-turn speculative inflight-key mismatch** | `consume()` waits on a `qh`-less `fetch_key` that can't match `schedule()`'s `qh`-keyed claim, so a still-in-flight speculative fetch isn't awaited and a redundant live fetch fires (wasted work, not a wrong answer). Align the keys. | 🟠 |
| 8 | **Switch-detection drift signal** | The mid-conversation SOP-switch trigger is now conservative (fires only on a non-empty out-of-vocab state — rarely). If real mid-call topic switching matters for a customer, design a proper drift signal (classifier uncertainty, periodic re-route check) instead of the vocab heuristic. Also: switch currently runs `classify_and_propose` twice (one result discarded). | 🟠 |
| 9 | **Claim-leak on `pool.insert` exception** | `_run_fetch`: if the pool insert raises (Redis hiccup), a speculative inflight claim isn't released until TTL, and the live-fallback path propagates the exception up. Wrap the insert. LOW (self-heals). | 🟢 |

## Tier 3 — Product completeness & integration surface

| # | Item | Notes | Tag |
|---|---|---|---|
| 10 | **`instruction_prefetch` as a per-project knob** | Currently deployment-wide env. Should be project-level like D-9 `subsystems` (pre-drafts help repeat-shaped flows, hurt open Q&A — proven on AENA). | 🟠 |
| 11 | **MCP server surface** | D-11 committed to MCP as an *integration surface* (one entry tool, routed by us) — not yet built. The path for customers with existing agent platforms to call SOPilot. | 🟠 |
| 12 | **Studio: project-settings UI for modes** | Advisory/strict + subsystems are set via API/PATCH; surface them in the Studio project view. | 🟢 |
| 13 | **API-doc drift guard** | Wire `scripts/gen_api_reference.py` into `scripts/check.sh` (and a pre-push step) so `API_REFERENCE.md` + `openapi.json` never drift from the code. | 🟢 |
| 14 | **Onboarding toolkit generalization** | `intents.py`/`mine_sop.py` hardcode the airport lexicon/topics as the reference instance; make them fully config-driven from the onboarding config so a new domain needs no code edits (the orchestrator provisioning is already generic). | 🟢 |
| 14b | **Prompt blocks: stage-scoped application in advisory** *(from prompt-block work)* | D-7 approved-wording blocks are bound per-stage, but advisory mode currently injects ALL of a session's pinned blocks as house wording (it doesn't hard-gate stages, and the reply is generated in parallel with classification so the stage isn't known yet). Fine for AENA's small set; at scale, apply only the classified stage's blocks (e.g. serialize a fast classify → stage-scoped reply, or a two-pass advisory) so wording stays precisely stage-relevant. Gated mode already does this. | 🟢 |
| 14c | **Prompt blocks in the onboarding config** *(from prompt-block work)* | `onboard.py` already provisions blocks from `prompts_file` and the SOP definitions carry the bindings, but the *binding map* (which block on which stage) currently lives in `bind_prompts.py` as the reference instance. Fold the stage→block mapping into the onboarding config so a new customer declares their approved wording + placement without code. | 🟢 |
| 14a | **Multilingual long-tail support** *(EDA lead)* | The dataset analysis quantified a real minority beyond the Spanish/English majority — French, Welsh, Malay/Indonesian, Italian fragments (isolated as distinct clusters, `docs/AENA_DATASET_ANALYSIS.html`). Today the agent is built/tested on es/en. Decide the policy: (a) the underlying models already handle these languages — verify the router + responder degrade gracefully and answer in-language; (b) if volume warrants, add lexicon/eval coverage for the top tail languages. Cheap to check, concrete demand evidence in hand. | 🟢 |

## Tier 4 — Production/ops maturity

| # | Item | Notes | Tag |
|---|---|---|---|
| 15 | **Monitoring & alerting** | Structured logs + per-tenant metrics exist; add aggregation/dashboards/alerts (supervisor lag, error rates, quota breaches, connector health) for a run-it-in-prod posture. | ⚙️ |
| 16 | **Load & latency testing at scale** | Characterize p95 under concurrent load; the known cold-first-turn latency (router + first retrieval, no prediction yet) is the headline number to profile and optimize. | ⚙️ |
| 17 | **Auth / exposure review** | Rate-limit and key-rotation review before any public-facing exposure; secrets/Fernet key rotation runbook. | ⚙️ |
| 18 | **Historical trace backfill (optional)** | Pre-fix traces lack response_text/embedding; a one-time backfill (~1k embedding calls) if the accumulated history is worth mining for pre-drafts/prediction. | 🟢 |

## Tier 5 — External proof & narrative (opportunistic)

| # | Item | Notes | Tag |
|---|---|---|---|
| 19 | **Stronger-model SOPBench arms on more domains** | gpt-4o university+healthcare done; Bank/others deferred as "question answered." Run if a bigger claim table is wanted. | 🟢 |
| 20 | **External writeup** | The claim set is complete (full benchmark +13.1 pp, adversarial +11.1, supervised-mini ≈ 71–88% of a frontier upgrade, procedure violations −57/−93%, real-customer +17 pp SOP effect). Package as a paper/blog if desired. | 🟢 |

---

## How to use this

- The tracked task list (`TaskList`) is the working queue for the current
  session; this file is the durable backlog it's drawn from.
- Tier 1 is the AENA go-live critical path — most items need customer inputs,
  not more platform code.
- Tiers 2–3 are the real engineering backlog for the next development cycle.
- Keep this file current: when a next-step is discovered mid-work, add it here
  rather than leaving it in a commit message.
