// Record the public replay: drive the real UI through every scene on a fresh server, then save trace.json.
// usage: node bench/record_replay.mjs <out.json> [base=http://127.0.0.1:8080]   (start the server just before, so
// the spend in the file is this recording only). VIDEO_DIR=<dir> also records the screen (1600x900 webm) and writes
// <dir>/marks.json with the second each scene started, for editing.
import { chromium } from 'playwright-core';
import fs from 'node:fs';

const OUT = process.argv[2], BASE = process.argv[3] || 'http://127.0.0.1:8080';
const LLM = ['tokens', 'header', 'link', 'form', 'lang'];
const st = async () => (await (await fetch(BASE + '/api/state')).json()).run;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const VIDEO = process.env.VIDEO_DIR;
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 },
  ...(VIDEO ? { recordVideo: { dir: VIDEO, size: { width: 1600, height: 900 } } } : {}) });
const page = await ctx.newPage();
const t0 = Date.now(), marks = [];
const mark = (label) => marks.push({ s: Math.round((Date.now() - t0) / 100) / 10, label });

async function until(what, ok, ms = 15 * 60e3) {
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
mark('scan baseline');
await page.click('#btn-scan-baseline');
let run = await until('baseline scan', (r) => r.versions[0].scan);
console.log('baseline', run.versions[0].scan.total_violations);

for (const scene of ['scene-a', 'scene-a2', 'scene-b']) {
  const n = nVersions(run);
  mark(scene);
  await page.click(`#btn-${scene}`);
  run = await until(scene, (r) => nVersions(r) > n && lastScored(r));
  const v = run.versions[Math.max(...Object.keys(run.versions).map(Number))];
  console.log(scene, 'axe', v.scan.total_violations, 'verdict', v.eval.verdict);
}

mark('run all');
await page.click('#btn-run-all');
run = await until('all groups', (r) => LLM.every((k) => ['approved', 'handoff'].includes(r.groups[k].status) ||
  (r.groups[k].status === 'retry_needed' && r.groups[k].retries_used >= 2)) && Object.keys(r.alt_candidates || {}).length);
console.log('groups', LLM.map((k) => `${k}=${run.groups[k].status}`).join(' '));

mark('apply alts');
await page.click('#btn-apply-alts');
run = await until('alt verification', (r) => r.groups['image-alt'].candidate_version && r.versions[r.groups['image-alt'].candidate_version]?.eval);
const altV = run.groups['image-alt'].candidate_version;
if (run.versions[altV].eval.all_pass) {  // the recording's human step: accept the pre-selected alt candidates
  await fetch(`${BASE}/api/run/${run.run_id}/approve/${altV}`, { method: 'POST' });
}

mark('scene D');
await page.click('#btn-scene-d');
run = await until('scene D', (r) => r.aviato_diagnosis, 4 * 60e3);
console.log('scene D', run.aviato_findings?.length ?? 0, 'findings');

// WCAG sources (Tavily) for every scored version, so the replay shows them without any live call
for (const v of Object.keys(run.versions).map(Number).filter((v) => v > 0)) {
  const c = await (await fetch(`${BASE}/api/run/${run.run_id}/citations/${v}`)).json();
  console.log('citations v' + v, (c.citations || []).map((x) => `${x.sc}:${x.source.split(' ')[0]}`).join(' '));
}
const trace = await (await fetch(`${BASE}/api/run/${run.run_id}/trace.json`)).text();
fs.writeFileSync(OUT, trace);
console.log('saved', OUT, (trace.length / 1024).toFixed(0) + 'KB');
mark('done');
if (VIDEO) fs.writeFileSync(`${VIDEO}/marks.json`, JSON.stringify(marks, null, 1));
await ctx.close();
await browser.close();
