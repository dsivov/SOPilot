# Config Manager — end-to-end scenario (reproducible)

A full three-stage walkthrough the PolarTie team can re-run to validate the
config manager and confirm every stage is **persisted (DB-backed) and restores
after an API restart**.

```
Stage 0  System discovery      → analysis report JSON   (/config/analysis)
Stage 1  Admin defines bounds  → schema (DSL) + rules   (/config/ruleset)
Stage 2  Customer authors      → config within bounds   (/config/document)
```

All three artifacts are versioned in Postgres per tenant/project (immutable
versions + a published pointer), so they survive process restarts and deploys.

## Prerequisites
- A running SOPilot instance (`<base>` = e.g. `http://127.0.0.1:8100`, or
  `https://<host>/api` behind the Studio proxy).
- The deployment admin token (`SOPILOT_ADMIN_TOKEN`).
- This repo checked out (for the script and the sample report).

## A. Automated run + restart-survival proof

```bash
# 1) run all three stages and verify persistence
python scripts/e2e_config_manager.py \
    --base http://127.0.0.1:8100 \
    --admin-token "$SOPILOT_ADMIN_TOKEN" \
    --tenant pt-e2e --project robot1

# 2) restart the API service (systemctl restart sopilot-api, or redeploy)

# 3) prove nothing was lost — re-fetch only, no writes
python scripts/e2e_config_manager.py \
    --base http://127.0.0.1:8100 --admin-token "$SOPILOT_ADMIN_TOKEN" \
    --tenant pt-e2e --project robot1 --verify-only
```

Both runs end with `ALL CHECKS PASSED`. The script creates the tenant/project
if missing, imports the pt-multirobot analysis report
(`docs/examples/pt-multirobot-analysis.json`), publishes a schema + a rule,
publishes a customer config, then re-fetches all three and asserts they match.
`--verify-only` skips writes — run it *after* the restart. (It never deletes the
tenant; remove it from the admin console when done.)

## B. Manual walkthrough in the Studio (the same scenario, by hand)

Log into the **admin console** (deployment admin token), create/enter the
tenant, then:

**Stage 0 — discovery.** Produce the analysis report by running the
`system-analysis` skill (or the [playbook](STAGE0_ANALYSIS_PLAYBOOK.md)) over the
target codebase, e.g. `../pt-multirobot` → validates to
`docs/examples/pt-multirobot-analysis.json`. In **Config admin → Available
options — schema (DSL)** click **Import analysis report…** and pick that file.
The schema is seeded (17 fields, 39 tools, 3 structures), the **System
architecture** diagram renders, and **Open questions for Engineering** lists the
unknowns (with **Export for Engineering**).

**Stage 1 — bounds.** Curate the schema (types, enum values, required,
descriptions), author a rule (e.g. *if `send_email` then require
`notification_service_url`*), then **Save & publish**.

**Stage 2 — customer config.** In **Config viewer**: **Load example** (or start
empty), then edit within bounds — change `voice`, enable tools, add an MCP
server (or *+ from connector*), add a transfer topic, write a prompt, ask the
assistant ("let the agent send verification SMS"). The editor is gated by the
published schema + rules: clearing `notification_service_url` while `send_email`
is on shows a **blocking** violation and disables **Apply changes** (with a
one-click *Disable send_email* fix). **Save & publish** the config.

**Restart check.** Restart the API, log back in, open Config admin and Config
viewer — the schema/rules and the config (with its version chip) reload from the
database exactly as published.

## What "persisted" means here
- **Stage 0** → `config_analysis_reports` / `…_versions`
- **Stage 1** → `config_rulesets` / `…_versions` (rules + `config_schema`)
- **Stage 2** → `config_documents` / `…_versions`

Each has a `published_version` pointer; the Studio and the write-back
(`/config/render-robot`) read the published version. The browser only holds a
draft cache — the database is the source of truth.
