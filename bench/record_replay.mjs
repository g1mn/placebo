// Record the public replay: drive the real UI through every scene on a fresh server, then save trace.json.
// usage: node bench/record_replay.mjs <out.json> [base=http://127.0.0.1:8080]   (start the server just before, so
// the spend in the file is this recording only)
import { chromium } from 'playwright-core';
import fs from 'node:fs';

const OUT = process.argv[2], BASE = process.argv[3] || 'http://127.0.0.1:8080';
const LLM = ['tokens', 'header', 'link', 'form', 'lang'];
const st = async () => (await (await fetch(BASE + '/api/state')).json()).run;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage();

async function until(what, ok, ms = 8 * 60e3) {
  const t0 = Date.now();
  for (;;) {
    const run = await st();
    if (ok(run)) return run;
    if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`);
    await page.waitForTimeout(1500);
  }
}
const nVersions = (run) => Object.keys(run.versions).length;
const lastScored = (run) => { const v = run.versions[Math.max(...Object.keys(run.versions).map(Number))]; return v.scan && (v.eval || v.version === 0); };

await page.goto(BASE + '/');
await page.waitForTimeout(1500);
await page.click('#btn-reset');
await page.waitForTimeout(800);
await page.click('#btn-scan-baseline');
let run = await until('baseline scan', (r) => r.versions[0].scan);
console.log('baseline', run.versions[0].scan.total_violations);

for (const scene of ['scene-a', 'scene-a2', 'scene-b']) {
  const n = nVersions(run);
  await page.click(`#btn-${scene}`);
  run = await until(scene, (r) => nVersions(r) > n && lastScored(r));
  const v = run.versions[Math.max(...Object.keys(run.versions).map(Number))];
  console.log(scene, 'axe', v.scan.total_violations, 'verdict', v.eval.verdict);
}

await page.click('#btn-run-all');
run = await until('all groups', (r) => LLM.every((k) => ['approved', 'handoff'].includes(r.groups[k].status) ||
  (r.groups[k].status === 'retry_needed' && r.groups[k].retries_used >= 2)) && Object.keys(r.alt_candidates || {}).length);
console.log('groups', LLM.map((k) => `${k}=${run.groups[k].status}`).join(' '));

await page.click('#btn-apply-alts');
run = await until('alt verification', (r) => r.groups['image-alt'].candidate_version && r.versions[r.groups['image-alt'].candidate_version]?.eval);
const altV = run.groups['image-alt'].candidate_version;
if (run.versions[altV].eval.all_pass) {  // the recording's human step: accept the pre-selected alt candidates
  await fetch(`${BASE}/api/run/${run.run_id}/approve/${altV}`, { method: 'POST' });
}

await page.click('#btn-scene-d');
run = await until('scene D', (r) => r.aviato_diagnosis, 4 * 60e3);
console.log('scene D', run.aviato_findings?.length ?? 0, 'findings');

const trace = await (await fetch(`${BASE}/api/run/${run.run_id}/trace.json`)).text();
fs.writeFileSync(OUT, trace);
console.log('saved', OUT, (trace.length / 1024).toFixed(0) + 'KB');
await browser.close();
