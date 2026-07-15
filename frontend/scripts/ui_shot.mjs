// UI verification: screenshots with real data loaded (network-idle waits).
// Usage: node scripts/ui_shot.mjs <base_url_with_query> <outdir>
import { chromium } from "playwright";

const base = process.argv[2] ?? "http://127.0.0.1:5174/";
const outdir = process.argv[3] ?? ".";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

async function shot(url, name, actions = async () => {}) {
  await page.goto(url, { waitUntil: "networkidle" });
  await actions();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${outdir}/${name}.png` });
  console.log("shot:", name);
}

const sep = base.includes("?") ? "&" : "?";
await shot(`${base}${sep}theme=light`, "pw_sops_light", async () => {
  const row = page.locator("table tbody tr").first();
  if (await row.count()) await row.click();
  await page.waitForTimeout(800); // lint debounce
});
await shot(`${base}${sep}theme=dark`, "pw_sops_dark", async () => {
  const row = page.locator("table tbody tr").first();
  if (await row.count()) await row.click();
  await page.waitForTimeout(800);
});
await shot(`${base}${sep}theme=dark`, "pw_blocks_dark", async () => {
  await page.getByRole("button", { name: "Prompt blocks" }).click();
  await page.waitForTimeout(500);
});
await shot(`${base}${sep}theme=light`, "pw_sessions_light", async () => {
  await page.getByRole("button", { name: "Sessions" }).click();
  await page.waitForTimeout(500);
});

await browser.close();
