# AENA Handover Package — Manifest

A content-free record of the customer handover package (the package itself is
real customer data and is gitignored / delivered by side channel; its full
recipe is committed: `use_case/analysis/{mine_sop,bind_prompts}.py`,
`use_case/onboarding/onboard.py`, `use_case/onboarding/aena.json`).

**Build:** `AENA_SOPilot_Handover_20260721.tar.gz` · 2050 KB · rebuilt after comprehensive prompt-block binding.

## Contents

| file | bytes |
|---|---|
| `README.md` | 6,526 |
| `agent/facts.jsonl` | 22,829 |
| `agent/onboard.py` | 11,556 |
| `agent/onboarding.json` | 953 |
| `agent/prompts.json` | 2,155 |
| `agent/sop_definitions/airport_services_and_facilities.json` | 7,316 |
| `agent/sop_definitions/flight_check-in_and_boarding_information.json` | 9,465 |
| `agent/sop_definitions/ground_transport_parking_and_wayfinding.json` | 10,306 |
| `agent/sop_definitions/lost_or_delayed_luggage.json` | 9,163 |
| `agent/sops/sop_airport_services.txt` | 3,374 |
| `agent/sops/sop_flight_info.txt` | 2,829 |
| `agent/sops/sop_lost_luggage.txt` | 3,330 |
| `agent/sops/sop_transport_wayfinding.txt` | 3,418 |
| `data/cleaned_conversations.jsonl` | 5,890,599 |
| `data/corpus_survey.json` | 972 |
| `data/topic_distribution.json` | 1,418 |
| `docs/API_REFERENCE.md` | 14,864 |
| `docs/INSTALL.md` | 9,672 |
| `report/REPORT_AENA_PROD.html` | 451,226 |

## State captured in this build
- **4 procedures** (v6): lost luggage, flight/boarding, transport/wayfinding, airport services & facilities.
- **7 prompt blocks** (`prompts.json`), placed by the config's `prompt_bindings` rules across every stage of all 4 SOPs (24 bindings). SOP definitions ship **binding-free** — placement is config-driven (14c), changeable without editing procedures.
- **111-fact** knowledge base (corpus-backed by default; Context Graph optional).
- Connectors: `airport-facts` (rag→corpus), `flight-status` (mock).
- Prompt-impact measured (advisory, 48 held-out scenarios, on vs off): coverage/concreteness flat, satisfaction 4.06→4.38 (+0.32, stage-scoped/14b), +170ms — real satisfaction lift, quality ≥ all-blocks.
- Secret scan: 0 keys, 0 internal hosts/paths.
