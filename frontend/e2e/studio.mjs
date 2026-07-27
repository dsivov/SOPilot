// Headless Studio click-through: verify the admin console, then drive the
// guided config editor (stage-2 gate: violations block Apply, fixes restore).
//
// IMPORTANT: this harness authenticates with a DEDICATED named key (minted via
// the admin API and injected into localStorage), NOT the one-click "Log in →"
// flow — that mints a single-active console-login key and would revoke the
// session of anyone using the Studio for this tenant. Minting a named key
// revokes nothing, so tests never disturb a live user.
//
// Needs the full stack running (backend :8100 + vite :5174, see README) and a
// tenant with the slug/display name below. Run from frontend/:  node e2e/studio.mjs
// Env: SOPILOT_STUDIO_URL, SOPILOT_ADMIN_TOKEN, SOPILOT_E2E_TENANT (display), SOPILOT_E2E_SLUG
import { chromium } from "playwright";

const BASE = process.env.SOPILOT_STUDIO_URL || "https://localhost:5174";
const API = process.env.SOPILOT_API_URL || "http://127.0.0.1:8100";
const ADMIN_TOKEN = process.env.SOPILOT_ADMIN_TOKEN || "dev-admin-token-p0";
const TENANT_NAME = process.env.SOPILOT_E2E_TENANT || "AENA — Malaga Airport";
const TENANT_SLUG = process.env.SOPILOT_E2E_SLUG || "aena";
let failures = 0;
const ok = (name, cond) => { console.log((cond ? "  ✔ " : "  ✖ ") + name); if (!cond) failures++; };

// Node fetch against the API (self-signed ok via NODE_TLS_REJECT_UNAUTHORIZED for https targets).
if (API.startsWith("https")) process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
const adminReq = (method, path, body) => fetch(API + path, {
  method, headers: { "X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json" },
  body: body ? JSON.stringify(body) : undefined,
}).then((r) => r.json());

// Mint a dedicated named key + resolve the tenant's first project.
const keyResp = await adminReq("POST", `/admin/tenants/${TENANT_SLUG}/keys`, { label: "e2e-harness", role: "admin" });
const projects = await adminReq("GET", `/admin/tenants/${TENANT_SLUG}/projects`);
const E2E_KEY = keyResp.api_key, E2E_PROJECT = projects[0]?.slug;

const browser = await chromium.launch();
const page = await browser.newPage({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 900 } });
page.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 120)));

// ---- 1. Admin console renders (no click on Log in → — that would clobber real sessions) ----
await page.goto(BASE);
await page.evaluate(() => localStorage.clear());
await page.goto(BASE);
await page.getByText("Platform admin →").click();
await page.getByPlaceholder("admin token").fill(ADMIN_TOKEN);
await page.getByRole("button", { name: "Enter" }).click();
await page.getByText("Create tenant").waitFor({ timeout: 5000 });
ok("admin console opens with token", true);
await page.getByText(TENANT_NAME).scrollIntoViewIfNeeded();
ok("target tenant visible (console scrolls)", await page.getByText(TENANT_NAME).isVisible());

// ---- 2. Enter the Studio with the named key (localStorage), not one-click login ----
await page.evaluate(([k, p]) => { localStorage.setItem("sopilot-api-key", k); localStorage.setItem("sopilot-project", p); },
  [E2E_KEY, E2E_PROJECT]);
await page.goto(BASE);
await page.getByText("Config viewer").waitFor({ timeout: 8000 });
ok("named-key session lands in Studio", true);

// ---- 3. Guided edit (user stage) ----
await page.getByText("Config viewer").click();
await page.getByText("Guided edit").waitFor({ timeout: 5000 });
const card = page.locator(".card", { hasText: "Guided edit" });
// The viewer defaults to an empty working config now; load the example so there
// is a real config (tools, fields) to exercise the guided editor against.
await page.getByRole("button", { name: "Load example" }).click();
await page.waitForTimeout(300);
const applyBtn = page.getByRole("button", { name: "Apply changes" }).first(); // header button (a 2nd appears in the dirty banner)
ok("Apply disabled when clean (no edits)", await applyBtn.isDisabled());

// Enable send_email — compliant while notification_service_url is set in the example config.
await card.locator(".chip", { hasText: /^send_email$/ }).first().click();
await page.waitForTimeout(300);
ok("Apply enabled after a compliant edit", await applyBtn.isEnabled());

// Clear notification_service_url → the requires-rule fires and blocks Apply.
await card.locator("label", { hasText: "notification_service_url" }).locator("input").fill("");
await page.waitForTimeout(300);
ok("blocking chip shown after violating edit", (await page.locator(".chip.crit", { hasText: "blocking" }).count()) > 0);
ok("Apply BLOCKED on error-level violation", await applyBtn.isDisabled());
ok("violation offers derived fix", (await page.getByRole("button", { name: /Disable send_email/ }).count()) > 0);

// One-click fix → back within bounds → Apply.
await page.getByRole("button", { name: /Disable send_email/ }).first().click();
await page.waitForTimeout(300);
ok("derived fix restores bounds, Apply re-enabled", await applyBtn.isEnabled());
await applyBtn.click();
await page.waitForTimeout(300);
ok("apply lands (editor back to clean)", await applyBtn.isDisabled());

// ---- 4. Global SOPilot copilot (the unified assistant; real model call) ----
// The per-tab Config/Connector assistants were folded into this one panel.
await page.getByRole("button", { name: /SOPilot copilot/ }).click();  // open the app-wide copilot
const chatInput = page.locator("input[placeholder^='Ask about']");
await chatInput.fill("how do I add weather data to the agent?");
await chatInput.press("Enter");
await page.waitForTimeout(22000);
ok("copilot answers a help question", (await page.getByText(/weather|connector|MCP|knowledge/i).count()) > 0);

// A concrete, in-bounds change should come back as an applyable proposal.
await chatInput.fill("set the voice to echo");
await chatInput.press("Enter");
await page.getByRole("button", { name: "Apply to editor" }).first().waitFor({ timeout: 45000 });
ok("copilot proposes an applyable change", (await page.getByRole("button", { name: "Apply to editor" }).count()) > 0);
ok("history retained across turns", (await page.getByText(/how do I add weather data/i).count()) > 0);
await page.getByRole("button", { name: "Close" }).click();  // close the copilot so it doesn't overlay later steps
await page.waitForTimeout(200);

// ---- 5. Complex structures: adding a KB without its backend must block ----
// (example config has neither opensearch_endpoint nor lightrag.postgres —
// a new KB row violates the simple-kb-needs-opensearch rule immediately)
await card.getByRole("button", { name: "+ Add knowledge base" }).click();
await page.waitForTimeout(300);
ok("KB without backend → blocking violation", (await page.locator(".chip.crit", { hasText: "blocking" }).count()) > 0);
ok("Apply blocked by structure edit", await applyBtn.isDisabled());
await card.locator("button[title='Remove knowledge base']").last().click();
await page.waitForTimeout(300);
ok("removing the KB clears the violation", (await page.locator(".chip.crit", { hasText: "blocking" }).count()) === 0);

// ---- 6. Derived field vocabulary: fields come from the config, advanced hidden by default ----
ok("fields derived from config (voice shown)", (await card.locator("span.mono", { hasText: /^voice$/ }).count()) > 0);
const advToggle = card.getByRole("button", { name: /Show advanced/ });
ok("advanced fields hidden behind a toggle", (await advToggle.count()) > 0);
ok("plumbing hidden by default (rem_ws_host absent)", (await card.locator("span.mono", { hasText: "rem_ws_host" }).count()) === 0);
await advToggle.click();
await page.waitForTimeout(200);
ok("toggle reveals advanced plumbing (rem_ws_host)", (await card.locator("span.mono", { hasText: "rem_ws_host" }).count()) > 0);

// ---- 7. Persistence: an applied edit survives a tab switch (remount) ----
// Regression: the mount fetch used to clobber the local draft with the DB config,
// so "Apply changes" (without Save) then navigating away silently lost the edit.
// Change the voice field in the guided editor, Apply it to the working config
// (NOT saved to DB), then leave and re-enter the viewer.
const voiceLabel = card.locator("label").filter({ has: page.locator("span.mono", { hasText: /^voice$/ }) }).first();
const voiceSel = voiceLabel.locator("select");
if (await voiceSel.count()) await voiceSel.selectOption({ index: 1 });
else await voiceLabel.locator("input").first().fill("echo");
await page.waitForTimeout(300);
await applyBtn.click();  // Apply changes → writes the new voice into cfg (NOT saved to DB)
await page.waitForTimeout(300);
const beforeText = await page.locator("textarea.area.mono").first().inputValue();
const chosenVoice = (beforeText.match(/"voice"\s*:\s*"([^"]*)"/) || [])[1] || "";
ok("edit reaches the working config on Apply", chosenVoice.length > 0);
await page.locator("button.navitem", { hasText: "Config admin" }).click();   // leave the viewer (unmounts ConfigView)
await page.waitForTimeout(500);
await page.locator("button.navitem", { hasText: "Config viewer" }).click();  // return → remounts ConfigView
await page.getByText("Guided edit").waitFor({ timeout: 5000 });
await page.waitForTimeout(700);                       // let the DB /config/document fetch settle
const afterText = await page.locator("textarea.area.mono").first().inputValue();
ok("applied edit survives a tab switch (draft not clobbered by DB)", new RegExp(`"voice"\\s*:\\s*"${chosenVoice}"`).test(afterText));
// If a saved DB version exists, a divergent draft must be flagged "unsaved" so
// the user knows to Save; with no saved version there's nothing to be dirty
// against, so the chip legitimately won't show. Assert the intent either way.
const hasSavedVersion = (await page.locator(".chead .chip", { hasText: /^v\d/ }).count()) > 0;
const unsavedShown = (await page.locator(".chip.warn", { hasText: "unsaved" }).count()) > 0;
ok("unsaved draft flagged when a saved version exists", !hasSavedVersion || unsavedShown);

await browser.close();

// Revoke the harness key so it doesn't accumulate (best-effort).
try {
  const keys = await adminReq("GET", `/admin/tenants/${TENANT_SLUG}/keys`);
  for (const k of keys.filter((k) => k.label === "e2e-harness" && !k.revoked)) {
    await adminReq("POST", `/admin/tenants/${TENANT_SLUG}/keys/${k.id}/revoke`);
  }
} catch { /* cleanup is best-effort */ }

console.log(failures === 0 ? "ALL PASS" : `${failures} FAILURES`);
process.exit(failures ? 1 : 0);
