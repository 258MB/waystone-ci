// One screenshot of the page shown when the Visual C++ runtime is missing.
// node vcshot.js <browser exe> <url> <out png>
const p = require("puppeteer-core");
(async () => {
  const [exe, url, png] = process.argv.slice(2);
  const b = await p.launch({ executablePath: exe, headless: true });
  const g = await b.newPage();
  await g.setViewport({ width: 1100, height: 900 });
  await g.goto(url);
  await new Promise((r) => setTimeout(r, 4000));
  await g.screenshot({ path: png });
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
