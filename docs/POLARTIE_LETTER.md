# SOPilot ↔ PolarTie voice-agent integration — branches & how to test

Hi team,

We've built the SOPilot integration into your voice agent end-to-end and pushed
everything to **feature branches in your repos** (your `master` is untouched).
Two stages, both working today against our AENA / Málaga SOPs.

Full write-up (architecture + findings + results):
`SOPilot/docs/BLOG_SOPILOT_POLARTIE_INTEGRATION.html`.

---

## The branches

**pt-multirobot** (`git@github.com:PolarTie/pt-multirobot.git`)
- `d-sop-stage1-harness-scenarios` — Stage 1: harness scenarios that drive the
  real AENA info-desk SOPs (no product code change). See
  `tests/voice_agent_harness/scenarios/SOPILOT_STAGE1.md`.
- `d-sop-stage2-supervisor-ext` — Stage 2: the generic **supervisor MCP
  extension** in the voice agent. **Contains a submodule bump.**

**pt-ai-agent-mcp** (submodule, `git@github.com:PolarTie/pt-ai-agent-mcp.git`)
- `d-sop-stage2-supervisor-ext` — the reserved `SUPERVISOR_TOOL_NAME`.

Check out Stage 2 (submodule included):
```bash
git fetch origin
git checkout d-sop-stage2-supervisor-ext
git submodule update --init --recursive   # pulls the pt-ai-agent-mcp branch commit
```

---

## What the two stages are

- **Stage 1 — model-driven (zero code change).** SOPilot is added as an ordinary
  MCP server exposing one tool, `sop_guidance`. The model calls it each turn to
  get the SOP's stage steering. Works on stock `master` — just an `mcp_servers`
  entry.
- **Stage 2 — automatic supervisor (the `d-sop-*` branches).** If a connected
  MCP server advertises the reserved `polartie_ai_agent_supervisor` tool, the
  agent treats it as a supervisor: it switches to **client-driven turns** and,
  on each completed user transcript, calls the tool and applies the returned
  text as `session.update` instructions before letting the model respond. The
  tool is hidden from the model — no prompt change, the model never has to
  "decide" to call it.

SOPilot picks the SOP from the caller's words (intake router, ~1 turn) and
**re-routes mid-call** if the caller changes topic (e.g. parking → flight).

---

## How to test in a prod environment

### 1. SOPilot side — the MCP endpoint (we run this)
SOPilot serves the MCP surface at **`https://<sopilot-host>/mcp`** (mounted in
the API process). It runs in *supervisor mode* — it exposes only
`polartie_ai_agent_supervisor`. Our AENA instance is already up this way
(env: `SOPILOT_MCP_MOUNT=true SOPILOT_MCP_MODE=supervisor SOPILOT_API_KEY=…
SOPILOT_PROJECT=malaga`). For a Stage-1 (model-driven) trial instead, we flip
`SOPILOT_MCP_MODE=tool` so it exposes `sop_guidance`.

### 2. PolarTie side — connect the agent
Add the endpoint to the session's `mcp_servers`:
```json
{ "mcp_servers": [
    { "url": "https://<sopilot-host>/mcp", "authorization": "Bearer <token>" }
] }
```
- **Stage 2 (recommended):** run the agent from `d-sop-stage2-supervisor-ext`.
  On connect it auto-detects the supervisor tool, flips to client-driven turns,
  and steers every turn from SOPilot. **No prompt changes.** Just place a call
  and watch it follow the SOP (it will produce SOP-specific facts, e.g. the
  correct baggage-office location, that aren't in the prompt).
- **Stage 1 (stock master):** point at a `tool`-mode endpoint and add one line
  to the prompt — "consult the available SOP tool each turn and follow it."

### 3. Quick harness check (no live telephony)
From `tests/voice_agent_harness/` on the branch:
```bash
export VOICE_AGENT_HARNESS_DOCKER_EXTRA_ARGS="--add-host=host.docker.internal:host-gateway"
export SOPILOT_MCP_URL="https://<sopilot-host>/mcp"
SOP_PROCEDURE=lost_luggage ./run-docker scenarios/sop_supervisor_ext.py   # Stage 2
SOP_PROCEDURE=lost_luggage ./run-docker scenarios/sop_via_mcp.py          # Stage 1
```
(Procedures: `lost_luggage`, `flight_info`, `transport`, `airport_services`.
On SELinux-enforcing hosts add `--security-opt label=disable` to the extra-args.
The first container after a `docker build` can exceed the 10 s ready-timeout on
the cold import — just re-run warm.)

---

## Good to know
- **Fails safe.** If SOPilot hiccups or is unreachable, the supervisor returns
  benign guidance / the agent falls back to its base prompt — a bad turn never
  breaks the call.
- **Latency.** The supervisor runs no LLM call the agent has to wait twice for:
  it skips SOPilot's own responder (`steer_only`, ~26% faster/turn) and the
  switch check runs in parallel. One caution you raised holds — the extra
  per-turn hop can add latency; measure on your infra and we'll tune.
- **Transcript timing.** We trigger on the *transcription-completed* event, not
  speech-stopped, because (as you noted) the transcript lands async. The garbled-
  transcript case is still worth a better STT when a supervisor is attached.
- **Auth caveat.** The mounted `/mcp` uses SOPilot's own tenant creds
  server-side and does **not** yet authenticate the MCP client — fine on a
  private network for this trial; we'll add client auth before any public
  exposure.

Happy to jump on a call to run the first live one together.

— The SOPilot team
