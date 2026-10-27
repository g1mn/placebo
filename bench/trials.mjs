// Loop B: drive the real Placebo UI ("Run all groups", then apply the pre-selected alt candidates) N times.
// usage: node bench/trials.mjs <trials> <out.json> [base=http://127.0.0.1:8080]   (server running, Chrome installed)
import { chromium } from 'playwright-core';
import fs from 'node:fs';

const N = +process.argv[2] || 1, OUT = process.argv[3] || 'bench.json', BASE = process.argv[4] || 'http://127.0.0.1:8080';
const LLM = ['tokens', 'header', 'link', 'form', 'lang'];
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage();
const st = async () => (await (await fetch(BASE + '/api/state')).json());
const results = [];

for (let t = 1; t <= N; t++) {
  await page.goto(BASE + '/');
  await page.waitForTimeout(1500);
  await page.click('#btn-reset');
  await page.waitForTimeout(800);
  const spend0 = (await st()).spend?.total_usd || 0;
  const t0 = Date.now();
  await page.click('#btn-run-all');
  // run-all ends by drafting alt candidates (or a handoff); wait for that or 8 min
  let s;
  while (Date.now() - t0 < 8 * 60e3) {
    await page.waitForTimeout(3000);
    s = await st();
    const g = s.run.groups;
    const llmDone = LLM.every(k => ['approved', 'handoff'].includes(g[k].status) ||
      (g[k].status === 'retry_needed' && g[k].retries_used >= 1));
    if (llmDone && (Object.keys(s.run.alt_candidates || {}).length || g['image-alt'].status === 'handoff')) break;
  }
  const llmS = (Date.now() - t0) / 1000;
  // image-alt: apply the default-checked candidate per image (what a hurried human would do)
  let altOk = null;
  if (await page.locator('#btn-apply-alts').isVisible().catch(() => false)) {
    await page.click('#btn-apply-alts');
    for (let i = 0; i < 40; i++) {
      await page.waitForTimeout(1500);
      s = await st();
      const ver = s.run.versions[s.run.current_version];
      if (s.run.groups['image-alt'].candidate_version && ver?.eval) { altOk = !!ver.eval.all_pass; break; }
    }
  }
  s = await st();
  const run = s.run;
  const groups = {};
  for (const k of [...LLM, 'image-alt']) {
    const g = run.groups[k];
    groups[k] = { status: g.status, retries_used: g.retries_used, timeouts: g.timeouts, guard_rejections: g.guard_rejections,
      handoff_reason: g.handoff_reason, last_model: g.last_model, human_review_items: (g.human_review_items || []).length };
  }
  groups['image-alt'].alt_pass = altOk;
  const approvedV = run.versions[run.approved_version];
  const curV = run.versions[run.current_version];
  const failedDiffs = {};
  for (const [v, o] of Object.entries(run.versions)) {
    if (+v > 0 && o.eval && !o.eval.all_pass) failedDiffs[v] = await (await fetch(`${BASE}/api/run/${run.run_id}/diff/${v}.patch`)).text();
  }
  results.push({
    trial: t, run_id: run.run_id, failed_diffs: failedDiffs, llm_wall_s: llmS, spend_usd: +((s.spend?.total_usd || 0) - spend0).toFixed(4),
    baseline_total: run.versions[0].scan?.total_violations, approved_version: run.approved_version,
    approved_total: approvedV?.scan?.total_violations, current_total: curV?.scan?.total_violations,
    current_all_pass: curV?.eval?.all_pass ?? null,
    groups,
    trace: run.trace.map(x => ({ v: x.version, g: x.group_id, model: x.model, before: x.violations_before, after: x.violations_after,
      guard: x.guard_passed, hidden: x.hidden_check_passed, verdict: x.verdict, s: x.elapsed_s, usd: x.est_cost_usd, tok: x.tokens })),
    evals: Object.fromEntries(Object.entries(run.versions).filter(([v]) => +v > 0).map(([v, o]) => [v, o.eval ? {
      all_pass: o.eval.all_pass, failed: Object.entries(o.eval.conditions).filter(([, c]) => !c.passed).map(([n]) => n),
      hidden: o.eval.conditions['6_no_harm_hidden'].failures } : null])),
  });
  console.log(`trial ${t}: ${llmS.toFixed(0)}s $${results.at(-1).spend_usd}`, Object.entries(groups).map(([k, g]) => `${k}=${g.status}/${g.retries_used}`).join(' '));
  fs.writeFileSync(OUT, JSON.stringify(results, null, 1));
}
await browser.close();
