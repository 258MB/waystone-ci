// Phone check in Safari's engine (Playwright WebKit, headless, iPhone size, one browser, closed at the end):
// first load and offline cache, offline reload, one emergency, the chooser and an off-topic question, plus the
// install bar and the storage answer; screenshots light and dark in phone-r1/shots/webkit-*.png.
// Usage: node phone-r1/eval/webkit.mjs   (run it through the heavy-job guard)
// CI: WEB_DIR (the unzipped app), OUT_DIR (results and screenshots); playwright from the folder's node_modules.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const here = path.dirname(fileURLToPath(import.meta.url));
const ph = path.join(here, "..");
const web = process.env.WEB_DIR ? path.resolve(process.env.WEB_DIR) : path.join(ph, "web");
const shots = process.env.OUT_DIR ? path.resolve(process.env.OUT_DIR) : path.join(ph, "shots");
const require = process.env.WEB_DIR ? createRequire(import.meta.url) : createRequire(path.join(ph, "..", "website", "package.json"));
const { webkit, devices } = require("playwright");

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".wasm": "application/wasm",
  ".bin": "application/octet-stream", ".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" };
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (p.endsWith("/")) p += "index.html";
  const f = path.join(web, path.normalize(p));
  if (!f.startsWith(web) || !fs.existsSync(f)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(f)] || "application/octet-stream", "Cache-Control": "no-cache" });
  res.end(fs.readFileSync(f));
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://localhost:${server.address().port}/`;

const out = { engine: "webkit", base, steps: {} };
const browser = await webkit.launch({ headless: true });
out.browser_version = browser.version();
try {
  const ctx = await browser.newContext({ ...devices["iPhone 13"] });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const t0 = Date.now();
  await page.goto(base, { waitUntil: "load" });
  await page.waitForFunction(() => window.__engineInfo, null, { timeout: 120000 });
  out.steps.first_load = { engine_ready_ms: Date.now() - t0,
    install_bar: await page.evaluate(() => !document.querySelector("#installbar").hidden),
    engine_error: await page.evaluate(() => document.querySelector("#engineerr").hidden ? null : document.querySelector("#engineerr").textContent) };
  out.steps.service_worker = await page.evaluate(async () => {
    if (!("serviceWorker" in navigator)) return "no service worker support";
    try { await Promise.race([navigator.serviceWorker.ready, new Promise((_, j) => setTimeout(() => j(new Error("timeout")), 60000))]); } catch (e) { return "not ready: " + e.message; }
    for (let i = 0; i < 120 && !navigator.serviceWorker.controller; i++) await new Promise((r) => setTimeout(r, 500));
    return navigator.serviceWorker.controller ? "controlling" : "ready, not controlling";
  });
  out.steps.storage = await page.evaluate(async () => ({
    persist_api: !!(navigator.storage && navigator.storage.persist),
    persisted: navigator.storage && navigator.storage.persisted ? await navigator.storage.persisted() : null,
    standalone: window.matchMedia("(display-mode: standalone)").matches }));

  const ask = async (q) => {
    const t = Date.now();
    await page.evaluate((h) => { location.hash = h; }, "#/q/" + encodeURIComponent(q));
    await page.waitForFunction(() => document.querySelector("#main .page, #main .chooser, #main .notfound"), null, { timeout: 30000 });
    return { q, ms: Date.now() - t, text: await page.evaluate(() => document.querySelector("#main").innerText.slice(0, 160)) };
  };
  const qs = { emergency: "My dad has crushing chest pain and we are far from any hospital.",
    chooser: "Grandpa is going quiet and his lips look grey", offtopic: "What is the capital of France?" };
  out.steps.online = {};
  for (const [k, q] of Object.entries(qs)) out.steps.online[k] = await ask(q);

  // offline: stop the server, reload, ask again
  await new Promise((r) => { server.close(r); server.closeAllConnections && server.closeAllConnections(); });
  const t1 = Date.now();
  try {
    await page.evaluate(() => { window.__engineInfo = null; location.hash = "#/"; });
    await page.reload({ waitUntil: "load", timeout: 30000 });
    await page.waitForFunction(() => window.__engineInfo, null, { timeout: 120000 });
    out.steps.offline = { reloaded: true, engine_ready_ms: Date.now() - t1 };
    for (const [k, q] of Object.entries(qs)) out.steps.offline[k] = await ask(q);
  } catch (e) {
    out.steps.offline = { reloaded: false, error: String(e) };
  }

  fs.mkdirSync(shots, { recursive: true });
  const screens = [["home", "#/"], ["emergency", "#/q/" + encodeURIComponent(qs.emergency)], ["chooser", "#/q/" + encodeURIComponent(qs.chooser)],
    ["offtopic", "#/q/" + encodeURIComponent(qs.offtopic)], ["about", "#/about"], ["empage", "#/e/heart"]];
  // What is on the first screen, above the bottom tabs, without scrolling (iPhone 13: 390x844).
  out.fold = {};
  for (const [name, hash] of screens.filter((x) => ["emergency", "chooser", "empage"].includes(x[0]))) {
    await page.evaluate((h) => { location.hash = h; }, hash);
    await page.waitForTimeout(400);
    await page.evaluate(() => window.scrollTo(0, 0));
    out.fold[name] = await page.evaluate(() => {
      const dock = document.querySelector("#dock");
      const limit = dock ? dock.getBoundingClientRect().top : innerHeight;
      const b = (el) => el ? Math.round(el.getBoundingClientRect().bottom) : null;
      const first = document.querySelector("#main .riskbox.first");
      let firstLine = null;
      if (first) { const r = document.createRange(); r.selectNodeContents(first); const rects = [...r.getClientRects()]; firstLine = rects.length ? Math.round(rects[0].bottom) : null; }
      const choices = [...document.querySelectorAll("#main .chooser .emlist a")].map(b);
      const step1 = b(document.querySelector("#main .page .steps li, #main .steps li"));
      return { visible_until: Math.round(limit), choices, all_choices_visible: choices.length ? choices.every((y) => y <= limit) : null,
        do_first_first_line: firstLine, do_first_visible: firstLine !== null && firstLine <= limit,
        do_first_text: first ? first.textContent.slice(0, 90) : null, step1_bottom: step1, step1_visible: step1 !== null && step1 <= limit };
    });
  }
  out.shots = [];
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    for (const [name, hash] of screens) {
      await page.evaluate((h) => { location.hash = h; }, hash);
      await page.waitForTimeout(400);
      await page.evaluate(() => window.scrollTo(0, 0));
      const f = path.join(shots, `webkit-${name}-${scheme}.png`);
      await page.screenshot({ path: f });
      out.shots.push(f);
    }
  }
  out.errors = errors;
} finally {
  await browser.close();
  try { server.close(); } catch (e) {}
}
out.user_agent = out.user_agent || null;
fs.writeFileSync(process.env.OUT_DIR ? path.join(shots, "webkit.json") : path.join(ph, "eval", "webkit.json"), JSON.stringify(out, null, 1));
const brief = JSON.parse(JSON.stringify(out));
for (const s of ["online", "offline"]) for (const k of Object.keys(brief.steps[s] || {})) if (brief.steps[s][k] && brief.steps[s][k].text) brief.steps[s][k].text = brief.steps[s][k].text.slice(0, 70);
delete brief.shots;
console.log(JSON.stringify(brief, null, 1));
process.exit(0);
