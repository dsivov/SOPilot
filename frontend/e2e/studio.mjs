// Headless Studio click-through: admin console → one-click tenant login →
// guided config editing (stage-2 gate: violations block Apply, fixes restore).
//
// Needs the full stack running (backend :8100 + vite :5174, see README) and a
// tenant with the display name below. Run from frontend/:
//   node e2e/studio.mjs
// Env: SOPILOT_STUDIO_URL, SOPILOT_ADMIN_TOKEN, SOPILOT_E2E_TENANT (display name)
import { chromium } from "playwright";

const BASE = process.env.SOPILOT_STUDIO_URL || "https://localhost:5174";
const ADMIN_TOKEN = process.env.SOPILOT_ADMIN_TOKEN || "dev-admin-token-p0";
const TENANT_NAME = process.env.SOPILOT_E2E_TENANT || "AENA — Malaga Airport";
let failures = 0;
const ok = (name, cond) => { console.log((cond ? "  ✔ " : "  ✖ ") + name); if (!cond) failures++; };

const browser = await chromium.launch();
const page = await browser.newPage({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 900 } });
page.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 120)));

// ---- 1. Admin console ----
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

// ---- 2. One-click login ----
await page.locator("div", { has: page.getByText(TENANT_NAME) }).locator("button", { hasText: "Log in →" }).last().click();
await page.getByText("Config viewer").waitFor({ timeout: 8000 }); // Studio nav renders → logged in
ok("one-click login lands in Studio", true);

// ---- 3. Guided edit (user stage) ----
await page.getByText("Config viewer").click();
await page.getByText("Guided edit").waitFor({ timeout: 5000 });
const card = page.locator(".card", { hasText: "Guided edit" });
const applyBtn = page.getByRole("button", { name: "Apply changes" });
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

await browser.close();
console.log(failures === 0 ? "ALL PASS" : `${failures} FAILURES`);
process.exit(failures ? 1 : 0);
