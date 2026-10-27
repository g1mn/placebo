// Scan one staged version of a Placebo run in headless Chrome and post it to record-scan, exactly like the UI does.
// usage: node bench/scan.mjs <base> <run_id> <version> <nonce>   (needs Google Chrome installed)
import { chromium } from 'playwright-core';

const [base, run, v, nonce] = process.argv.slice(2);
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage();
// the verifier posts to window.parent; opened top-level, parent is the page itself
await page.addInitScript(() => addEventListener('message', e => { if (e.data?.type === 'soundcheck_scan_result') window.__r = e.data; }));
const pages = {};
for (const p of ['index.html', 'product.html', 'cart.html', 'checkout.html']) {
  try {
    await page.goto(`${base}/preview/${run}/v${v}/${p}?scan=1&nonce=${nonce}`);
    await page.waitForFunction(() => window.__r, null, { timeout: 20000 });
    pages[p] = await page.evaluate(() => window.__r);
  } catch (e) {  // a page that never renders is a verdict (runtime error), not a crash of the benchmark
    pages[p] = { error: `scan failed: ${String(e.message || e).slice(0, 120)}` };
  }
}
await browser.close();
const r = await fetch(`${base}/api/run/${run}/record-scan`, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ version: +v, pages }),
});
if (!r.ok) { console.error(`record-scan ${r.status}`); process.exit(1); }
