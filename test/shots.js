// Screenshots of the running guide: home, emergency grid, one emergency page, and answers to two questions.
// node shots.js <url> <browser exe> <out dir> <rule question> <generic question>
const puppeteer = require("puppeteer-core");
const path = require("path");
const [url, exe, out, ruleQ, genQ] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: exe, headless: true, args: ["--no-first-run", "--no-default-browser-check"] });
  const p = await b.newPage();
  await p.setViewport({ width: 1100, height: 900 });
  const shot = async (name, full) => { await p.screenshot({ path: path.join(out, name), fullPage: !!full }); console.log("shot", name); };
  await p.goto(url, { waitUntil: "networkidle2" });
  await sleep(4000);
  await shot("1-home.png");
  await p.click("#emergbtn");
  await sleep(1500);
  await shot("2-emergency-grid.png", true);
  const btns = await p.$$("#view-emerg .embtn");
  if (btns.length) { await btns[0].click(); await sleep(1500); await shot("3-emergency-page.png", true); }
  for (const [i, q] of [[4, ruleQ], [5, genQ]]) {
    await p.goto(url, { waitUntil: "networkidle2" });
    await sleep(2000);
    await p.type("#q", q);
    const t0 = Date.now();
    await p.click("#askbtn");
    try { await p.waitForFunction(() => document.querySelector("#answers .block, #answers .riskbox, #answers .card"), { timeout: 120000 }); console.log(`first view after ${(Date.now() - t0) / 1000} s`); } catch (e) { console.log("no first view", e.message); }
    await sleep(500);
    await shot(`${i}a-first-view.png`, true);
    try { await p.waitForFunction(() => !document.querySelector("#answers .thinking"), { timeout: 900000 }); console.log(`final view after ${(Date.now() - t0) / 1000} s`); } catch (e) { console.log("no final view", e.message); }
    await sleep(1000);
    await shot(`${i}b-final.png`, true);
  }
  // r21: optional extra questions, final view only (SHOTS_EXTRA = a JSON file of [name, question] pairs).
  const extra = process.env.SHOTS_EXTRA ? JSON.parse(require("fs").readFileSync(process.env.SHOTS_EXTRA, "utf8")) : [];
  for (const [name, q] of extra) {
    await p.goto(url, { waitUntil: "networkidle2" });
    await sleep(2000);
    await p.type("#q", q);
    const t0 = Date.now();
    await p.click("#askbtn");
    try { await p.waitForFunction(() => document.querySelector("#answers .card, #answers .riskbox, #answers .notfound") && !document.querySelector("#answers .thinking"), { timeout: 900000 }); console.log(`${name}: final view after ${(Date.now() - t0) / 1000} s`); } catch (e) { console.log(`${name}: no final view`, e.message); }
    await sleep(1000);
    await shot(`7-${name}.png`, true);
  }
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
