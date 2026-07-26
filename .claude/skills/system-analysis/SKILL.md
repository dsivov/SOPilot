---
name: system-analysis
description: Run a Stage-0 System Analysis on a target codebase and emit a validated sopilot-system-analysis report JSON that seeds SOPilot's Stage-1 config schema. Use when asked to "analyze a system/project for config management", "run system discovery / Stage 0", "produce an analysis report", or to onboard/re-analyze a system for the config manager.
---

# Stage-0 System Analysis

Produce a `sopilot-system-analysis` report JSON for a target system, so SOPilot's
Config admin can seed its Stage-1 schema from it.

## Do this

1. **Read the full procedure**: `docs/STAGE0_ANALYSIS_PLAYBOOK.md` (in this repo).
   Follow its steps exactly — it defines what to explore and the report contract.
2. **Explore the target codebase** (its path is given by the user, e.g.
   `../Context_Graph`): config templates (`.env.example`, `config.ini`,
   settings modules), `docker-compose`/deploy manifests, API routers, DB and
   LLM/provider config. Map: components (+ `exposes`), config_items
   (field/tool/structure with type/enum/required/status/source),
   integration_points (db/http with technology/target/`auth.secret_ref`), and
   `open_questions` for anything unresolved (`status:"needs_input"`).
3. **Write** the report to `docs/examples/<system>-analysis.json`.
4. **Validate** before finishing:
   `python scripts/validate_analysis_report.py docs/examples/<system>-analysis.json`
   — fix everything it reports; the run isn't done until it prints `✔ valid`.
5. **Report back** to the user: the file path, the validator summary (field/
   tool/integration counts, needs_input, open_questions), and how to load it
   (Config admin → *Import analysis report…*, or `PUT /config/analysis`).

## Rules
- Never put secret values in the report — credentials are `secret-ref` only.
- Use the system's real config paths; don't invent names.
- Flag unknowns as `needs_input` + an `open_question` rather than guessing.
- Worked examples to imitate: `docs/examples/context-graph-analysis.json`,
  `docs/examples/analysis-report.sample.json`.
