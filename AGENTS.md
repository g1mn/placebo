# Placebo — Agent Brief

> **Note (Oct 27, 2026):** this is the build brief from the Oct 16 Google DeepMind Seoul hackathon, when Placebo ran on Gemini. The model layer is now NVIDIA Nemotron on Nebius Token Factory (see the README section "Who does what" and `nebius_runtime.py`). The Gemini-specific rules below apply only with `MODEL_BACKEND=gemini`. The verifier, Guard and store rules still hold.

Read all of this before doing anything. It is the source of truth for this repo. If a request conflicts with it, follow the human's latest instruction.

## 1. Mission

- **Event**: Google DeepMind AI Hackathon, Seoul, 2026-10-16. Coding 11:20–15:20 KST. Solo. **Track 3** — Google AI for social good (accessibility). Fallback: Track 4 (developer tools) with the same build, only if the human decides so before 11:20.
- **Placebo** — "Accessibility fixes that can't fool the checker. Hear the difference."
- **Identity**: AI accessibility fixes can fool the checker. Placebo catches the trick and lets you hear what a screen-reader user actually hears change.
- **Loop**: axe-core scans every page of a small static site → Gemini 4 Argon diagnoses the shared root cause and patches the source (tokens, shared header/form/layout) → the whole site is re-verified with axe-core plus hidden "no harm" checks the patching model never sees → checker-fooling patches are rejected → a human approves → a simulated screen-reader readout plays before and after.
- **People**: helps blind shoppers using screen readers and keyboards; used by small-store developers and theme authors before a certification audit.
- **Done at 15:20**: public Cloud Run URL running baseline → patch → re-verify → approve → readout (plus a recorded replay); public GitHub repo with an English README listing only features that actually ran; 3-minute demo video uploaded; project description submitted.

## 2. Hard rules

- **Fixed directive**: Cloud Run only in the assigned project; no external DB/hosting; static HTML/CSS/vanilla JS only (no PHP); all UI strings, code comments, commit messages and README in English.
- **The demo store content is English too**: fictional store 'Bukchon Wool', prices in KRW, footer says 'A fictional store built for a demo.'
- **No autonomous agents in the product.** The app never redeploys, commits or pushes by itself. "Apply" = update /preview + offer a diff download. Every model run starts from a human click.
- **Static demo store**: every form uses action="#" plus JS preventDefault. Before committing site files, a case-insensitive search for ".php" and "<?php" must return zero hits.
- **Secrets**: never in git, logs, terminal output or UI. Secret Manager secrets `gemini-api-key` and `demo-token`, injected with --set-secrets as env `GEMINI_API_KEY` and `DEMO_TOKEN`; the runtime service account (default compute SA unless the human says otherwise) needs Secret Manager Secret Accessor on both — the human pre-approved this; grant it without asking. Locally only in .env (ignored).
- **The human supplies, never guess**: GCP project ID (given at check-in), repo name, demo token value, cost mode, and the input files — img/ (Nano Banana) and inputs/aviato (copied from a local cache). Never download them yourself.
- **Git**: first commit = .gitignore only (.env, .venv, __pycache__, *.mov, *.mp4). Right after git init, a pre-commit hook scans the staged diff for Google API keys ("AIza" + 35 key characters) and aborts with "KEY". Never bypass it (no --no-verify); if it fires, remove the offending line. Commit about every 30 minutes. Repo g1mn/soundcheck is PRIVATE until submission; the human makes it public at 14:55.
- **README disclosure, verbatim**: "Planning notes and throwaway feasibility checks (no product code) were prepared before kickoff; all code and the demo site were written after 11:20 KST (first commit HH:MM)".
- **Resource policy**: bounded retries and parallelism, nothing runs unattended, every model call has an output cap and a deadline.

## 3. Architecture

**Stack/deploy**
- FastAPI + one single-page HTML UI (vanilla JS; no Streamlit, no framework). Repo root: .python-version = 3.13, main.py exposing app, requirements.txt with fastapi, uvicorn, google-genai 2.30.1+ and never gunicorn (buildpacks would pick it and fail on ASGI). If the entrypoint is wrong, use --set-build-env-vars GOOGLE_ENTRYPOINT, not a Dockerfile.
- gcloud run deploy --source . --max-instances 1 --min-instances 1 --timeout 900 --concurrency 20 --no-invoker-iam-check, region asia-northeast3 (us-central1 if refused).
- **Single instance on purpose**: run state, previews, semaphore and spend live in memory. Never add Cloud Storage, a database or session affinity. A redeploy wipes runs; replays must be committed files.
- Model calls stream to the browser over SSE. Use the async client (client.aio) or run sync SDK calls in a thread; never block the event loop.
- After each deploy, confirm HTTP 200 in a logged-out incognito window.

**Demo store (input)**
- Small English-language storefront for a fictional Seoul shop (home, product, cart, checkout). UI label: "Planted defects — we built this store to break it".
- **Generation and commit order**: Generate once → rg checks → commit raw output with DEFECTS.md ('raw Argon output') → after /preview + verifier, compare axe to the expected baseline → hand-fix commit 'Align demo store with planted-defect spec' if needed; /reset restores the last of these two commits.
- **Generation prompt: STORE_PROMPT.md** (prepared before kickoff) — send it verbatim; never improvise the store. It fixes the files (≤ 300 lines each), shared partials, exact tokens, unique stable ids, no storage/cookies, and no violations beyond the planted six.
- **Include rule**: whole-line markers only (e.g. a line that is exactly `<!-- include: header.html -->`), one level, unknown name = render failure, wrap with <!-- begin: name --> / <!-- end: name --> comments. One fix in a shared file fixes every page.
- Six planted types, mostly in shared files: color-contrast (one token, --color-text-muted), button-name (header icon buttons .search-toggle / .menu-toggle), label (shared newsletter form), link-name (.cart-link + 2 .social a, on every page), html-has-lang (layout-top.html), image-alt (13 images incl. the banner). Baseline: 58 violation nodes, counted per (page, rule, selector).
- **Dual-context token trap**: --color-text-muted = #8C8C8C, used at 14px weight 400 on solid #FFFFFF in the body (3.36:1, fails 4.5:1) and on solid #1C1F26 in header/footer (4.90:1, passes). No gradients or background images. No single color fixes the body without breaking the dark context or vice versa as a naive darkening does (e.g. #767676 drops to 3.63:1 on #1C1F26); the real fix is splitting the token by role.
- The axe comparison after /preview must show: all six types present; html-has-lang on all 4 pages from layout-top.html alone; trap fails in the body and passes in header/footer; baseline 58.
- Product images (6) + one low-contrast text banner: made by the human with Nano Banana in AI Studio.

**Real input (scene D)**
- themefisher/aviato-bootstrap @ df6686f8cd7756393d65aae3fa23b9257985765a (MIT). Copy about 5 pages from theme/ plus css/style.css and LICENSE into inputs/aviato/. Strip every script tag before committing (jQuery plugins and a Google Maps script that embeds a third-party API key). Never its images. Backup: uakp98/shoe_store @ 30f34c38ec7521408d2e151d3b2fc78d765db98e (Apache-2.0, keep its LICENSE).
- Scan + diagnosis only, never patched. In this mode window.onerror is a warning, not a failure; axe "incomplete" items are listed separately.
- Three deterministic Placebo findings shown next to axe, labelled "Passes axe — flagged by Placebo": existing alt/aria-label values on the generic-name denylist; CSS that sets outline 0/none on :focus without a replacement; form controls named only by placeholder. Feed these plus the shared CSS to the Argon diagnosis.

**Verifier**
- axe-core 4.14.0 vendored (keep MPL header), served same-origin from /static. No CDN.
- /preview/{run}/v{n}/{page} with Cache-Control: no-store and CSP connect-src 'none'; form-action 'none'. All site assets (CSS, images, includes) resolve relative to /preview/{run}/v{n}/ so each version renders its own patched files.
- Server appends axe + a runner to each preview. Runner: load → document.fonts.ready → 2× requestAnimationFrame → axe runOnly tags wcag2a, wcag2aa, wcag21a, wcag21aa (no wcag22aa, no best-practice; same for the aviato scan) + hidden checks → postMessage to parent. Also reports window.onerror, load timeout, unfinished axe; wraps axe in try/catch and posts its own error text (cross-origin script errors only say "Script error.").
- iframe sandbox="allow-scripts" only, so the iframe origin is opaque: the runner posts with targetOrigin "*" and a per-run nonce; the parent accepts a message only if event.source is the preview iframe's contentWindow and the nonce matches (event.origin will be "null").
- The scan runs in the browser that has the Placebo UI open (presenter's or judge's); there is no server-side browser. Pages scanned one at a time. The model never sees verifier source, only result JSON. Violation keys (the counting unit) are (page, rule, selector); a model target given as {rule, selector} is expanded by the server to every page where that selector appears.

**Patch format**: whole-file for files ≤ 300 lines, otherwise SEARCH/REPLACE blocks that match exactly once in the last approved version; never unified diff (the server may generate one for the human's download). Backstop: reject when deleted or replaced lines exceed 40% of the original and the file has more than 30 lines; additions do not count.

**Six pass conditions (all → SUCCESS)**
1. Targeted (page, rule, selector) keys gone.
2. Zero new violations site-wide.
3. Zero serious/critical among the group's targeted keys and among any new keys; site-wide zero is only the run's 'done' marker.
4. No window.onerror, load timeout or unfinished axe.
5. Site-wide violations strictly below baseline.
6. **No harm (hidden)**: per page, counts of a/button/input/select, img count and visible text length do not drop. Visible text = text of elements with a non-zero bounding box inside the document area, not clipped (clip/clip-path) and not color-transparent. No newly added display:none, visibility:hidden, opacity:0, font-size:0, color:transparent, clip, clip-path, height:0 with overflow:hidden, or negative left/top/text-indent beyond -100px — counted relative to the baseline stylesheet (the baseline has one [hidden] rule). Never told to the model; a hidden-check failure hands the group off immediately (hidden_check_failed) with no retry feedback. UI shows "Patch model never saw this check".

**Guard (deterministic, runs on every patch)**
- The guard never replaces the scan: a rejected patch is still rendered and scanned, so the UI shows axe / guard / hidden check side by side. Exception: security rejections (outside_whitelist, script_or_csp_change, verifier_reference, id_changed) are never rendered; content rejections (generic_name, filename_name, presentational_content_image, placeholder_only_name) are staged, scanned and shown with verdict=rejected. Any rejection blocks SUCCESS and approval and counts toward HANDOFF.
- Whitelist: demo store HTML/CSS only.
- Reject: added script tags, on* event attributes, javascript: URLs, base, iframe, object, embed, new link tags other than the existing stylesheets, any axe/verifier string, CSP or meta http-equiv changes, changes to existing ids.
- Content images: reject new role="presentation" or role="none", alt="", aria-hidden="true" or the hidden attribute (axe passes all of these); decorative calls go to a human.
- Generic-name denylist (case-insensitive, trimmed) on every new name source — alt, aria-label, text referenced by aria-labelledby, title, placeholder, button/link text: image, photo, picture, icon, button, link, img, 이미지, 사진, 아이콘, 버튼, or a filename (*.jpg, *.png). A form control whose only new name is placeholder or title is rejected (placeholder_only_name).
- The html lang value must be 'en'; any other value is rejected.
- Rejection text names the trick, e.g. 'Rejected — alt="image" is not a description'.
- Alt text, color, focus order and visible wording always need human approval. The flash-lite wording guard can only add items to that list, never remove.

**State machine**
- One root-cause group at a time: patch → guard + re-scan → verdict.
- Argon handles the shared-source groups (tokens, header, form, link, lang). image-alt never goes through Argon: MODEL_ASSIST drafts 3 candidates per image (in parallel, fallback on 503), the human picks one, the server writes the alt deterministically, then guard + re-scan.
- HANDOFF, shown as "Escalated to human: <reason>" (never a stack trace): 2 retries used; no progress (immediately); hidden check failed (immediately, hidden_check_failed); 2 guard rejections; mode guide; 2 timeouts; cannot_fix.
- **No progress** = the site-wide violation total did not decrease (immediate HANDOFF). A patch that lowers the total but adds new violations goes back as retry feedback (scene B).
- Diagnosis fields: mode (patch | guide), escalation_reason, cannot_fix (bool). Honest give-up is normal output.
- Retry feedback: remaining keys + new violations + guard reasons only, never full axe JSON, never anything about the hidden check.
- "Run all groups" runs the same loop over the Argon groups sequentially (no new logic) and reuses alt text the human already picked; with none picked it stops at "Waiting for alt approval". It produces the results table.

**Demo safety and public URL**
- /reset restores the broken store (the last of the raw-output / hand-fix commits). ?replay=<run_id> replays a recorded run with "Replaying recorded run (HH:MM)"; replay files are trace JSON (including TTS audio, so replay never calls TTS) that the human downloads and commits under replays/. Also a "Load trace JSON" button that replays a local file.
- Banner "Step k/5": 1 Scan every page · 2 Diagnose & patch (Argon) · 3 Re-verify whole site · 4 Retry or hand off · 5 Approve & hear it. Step cards: waiting / running (elapsed s, model ID) / failed reason.
- Every trace line, diff card and results row shows the model that actually produced it; a fallback shows a visible "Fallback model: <model>" badge.
- Expensive endpoints need the demo token; Argon semaphore 2; cumulative spend cap $20 shared with the spend meter. Results and replays stay public. The landing page shows "Watch a recorded run" first and "Live runs need a demo token to cap spend; ask at our table." (README says the same).

## 4. Gemini call rules

- Interactions API, client.interactions.create (verified present in google-genai 2.31.0). Chain with previous_interaction_id but re-send tools, system_instruction and generation_config every turn.
- Manual function-call loop based on the official cookbook quickstarts/Function_calling.ipynb (Apache-2.0, credit it in README). No agent frameworks.
- **Live path**: Full source + baseline are in the prompt. First turn: update + submit_diagnosis + propose_patch together (1 hop target). read_file/run_scan exist but are described as rarely needed; max 3 hops per round. escalate_group is the honest give-up in retries. Tools (fixed order): escalate_group, propose_patch, read_file, run_scan, submit_diagnosis, update. The pass/fail re-verification always happens in the browser outside the model loop. On retry, the propose_patch function result carries the retry feedback.
- thinking_level: medium for diagnose+patch (THINKING_FIX default medium, other roles low when unset). Pin low if a rehearsal Argon ① run exceeds 75 s. **high is banned on the live path.**
- max_output_tokens ≥ 32768 on every Argon call. Never send temperature, top_p, top_k, thinking_budget or explicit cached content (400s).
- Fixed order for implicit caching: system_instruction → tools (fixed order/descriptions) → rules → source files (fixed sort) → variable part last. No timestamps or random IDs in the prefix. Show the usage.total_cached_tokens ratio for Argon calls only (short calls do not cache).
- Precautions (keep them even where docs allow more): send images as separate parts, not inside function results; never mix function results and text in one turn; never switch models mid-history (fallback = fresh interaction); never append to a running interaction.
- One wrapper: model and thinking level from env per role. **Budget**: DEMO_MODE=1: round cutoffs 85 s / 40 s, then immediate replay (no fallback model, no low re-call). DEMO_MODE=0: deadlines 120 s / 60 s, fallback chain gemini-4-argon → gemini-3.8-flash → gemini-3.6-flash as fresh interactions labelled 'Fallback model: …'; status incomplete → retry once at low. SDK retries max 2, only for fast 429/503, never after a deadline (each costs).
- Enable thinking summaries on Argon calls (thinking_summaries "auto"); when a step has no summary, show "thinking… (elapsed s)". Trace panel: one line per attempt (attempt, violations baseline → current, guard, hidden check, tokens/est. cost, seconds); trace JSON download.
- Env vars, never hard-coded: MODEL_FIX = gemini-4-argon (diagnose+patch); MODEL_REVIEW = gemini-4-argon (review call); MODEL_ASSIST = gemini-3.8-flash (3 alt candidates from the product image; frequent 503s); MODEL_ROUTER = gemini-3.5-flash-lite (wording guard); MODEL_TTS = gemini-3.8-flash-tts; one THINKING_* level per role; DEMO_MODE (0 while building; set 1 only after a replay file is committed).
- At the first call, confirm model IDs with client.models.list() before relying on env defaults. Develop against flash or recorded responses; real Argon calls only at integration points.
- **Cost mode** (human picks; default save): save = one patch candidate per group, no Best-of-N, no Argon review call. expand = Best-of-N 2 and the optional Argon review. Both keep retry cap 2 and no thinking high on the live path; a spend-related 429 drops to save.

## 5. Demo scenes (on camera — prioritise)

- **A. Cheating patch rejected**: "Inject a cheating patch" applies one fixed patch to the broken baseline, labelled "Pre-built cheating patch (not model output)", that fools axe on all six types: alt="image" on all 13 images incl. the banner; display:none on the 6 white-background muted selectors; aria-label="button" on .search-toggle/.menu-toggle and aria-label="link" on .cart-link and both .social a; placeholder="Email" only on #newsletter-email; lang="en" in layout-top.html — the only genuine fix. role="presentation" on a content image is a separate single-patch button (A2). The axe counter shows 0 (verify before recording); guard red ('Rejected — alt="image" is not a description'); hidden check red ("Patch model never saw this check"). No aria-hidden on focusable elements (axe catches that).
- **B. Regression caught**: 'Try a naive per-violation fix' applies a pre-built patch (--color-text-muted → #767676) to the baseline, labelled "Pre-built naive fix: darken the one failing color (one violation at a time, not model output)"; the whole-site re-scan goes red (12 new header/footer failures) and condition 2 rejects it. Then a separate, fresh Argon run is shown exactly as it happened. Never feed the scripted patch's result into an Argon chain. Narration must describe what actually happened in the recorded run.
- **C. Hear it**: "Simulated screen-reader readout (voiced by Gemini TTS)". In DOM order: the three header icon controls (Search button, Cart link, Menu button) and the first product photo on home. Empty name → role word only; otherwise the accessible name only. Before: 'button. link. button. image.' After: 'Search. Cart. Menu. Navy wool coat.' Label: 'Simulated readout: accessible name, or the role when the name is empty.' Extracted deterministically in the preview; never synthesize names that are not in the DOM.
- **D. Real input**: aviato scan → diagnosis card with the three "Passes axe — flagged by Placebo" findings, ~30 s.
- **E. (optional, no code)**: same site run locally with MODEL_* switched to gemini-3.8-flash; only if everything else is done; no comparison UI.
- **Budget**: scan 15 s → one Argon diagnose+patch round (cutoff 85 s, streamed) → re-scan 15 s → one retry (cutoff 40 s) → readout 10 s. Over a cutoff → immediate replay.
- Results table (from "Run all groups"): N → M violations, R regressions caught (source labelled, e.g. 'pre-built naive fix'); never say Argon regressed unless the recorded run shows it; G cheating patches rejected, K handed to a human, spend, cache hit %; always labelled "Run <id> · recorded HH:MM KST".

## 6. Build order

| KST | Goal |
|---|---|
| 11:20–11:35 | .gitignore commit + hook, public repo, secrets + accessor role, empty FastAPI deployed + incognito check, first Argon call (short timeout); human checks quota and picks cost mode (default: save) |
| 11:35–12:35 | Generate once → rg checks (.php, duplicate ids = 0) → commit raw output with DEFECTS.md ('raw Argon output') → /preview + verifier → compare axe to the expected baseline (6 types, trap, 58) → hand-fix commit 'Align demo store with planted-defect spec' if needed → one streamed diagnose+patch round → six conditions (incl. hidden check 6) + state machine |
| 12:35–13:05 | Approval screen: diff, before/after counts, pick 1 of 3 alt candidates |
| **13:05 gate** | **Baseline → patch → re-verify succeeded once on the deployed URL.** |
| 13:05–13:50 | In this order: (1) guard + cheating-patch button + naive-fix button (A, B) (2) TTS readout (C) (3) aviato scan + diagnosis card (D) (4) Run all groups + spend meter (5) flash-lite wording guard if time remains (if skipped, drop "Flash-Lite" from the video's model list) (6) Best-of-N 2 only if the human chose expand mode → redeploy |
| 13:50–14:40 | Feature freeze. Human records rehearsals and edits video. Only fix blockers. |
| 14:40–14:55 | Commit 1–2 replay JSON files under replays/, redeploy once (data only), confirm ?replay=<run_id> in incognito; README (only what ran); confirm .env and keys are not tracked; final push |
| 14:55–15:05 | Human submits. After that, no code changes. |

**If the gate fails at 13:05**: keep fixing the core loop until 13:35 at most, then build scenes A and C first (no Argon loop needed). Switching the live model to flash needs the human's OK. Never fake a verdict; unfinished steps show "Escalated to human: <reason>".

## 7. Do not build

- The Antigravity managed agent; thinking high on the live path.
- Keyboard task runs, a browser service, server Chromium, live-path screenshots, computer use.
- Agent frameworks, Streamlit, databases, Cloud Storage, external hosting, login/accounts, a second instance.
- A model that decides pass/fail; long-decode reports; a scene E comparison UI; extra pages or polish before the 13:05 gate.
- Patching the real template or adding its images or scripts.

## 8. Working agreement

- Small verifiable steps. After each, run it and show the real output (command, status code, scan count).
- Never claim success without running it; say "not verified" otherwise.
- **Do not stop to ask**: deploys, APIs, IAM, secrets and Argon calls in this project are pre-approved. Work to the end and report. Never delete projects; never commit secrets.
- Never print, echo or log API keys or tokens.
- Don't invent API parameters; check the installed Gemini API skill or ai.google.dev.
- Wording: never "compliant", "certified", "fully accessible", "one-click compliance". Say "axe violations N → M, K items need human review".
- Never write "won damages", "fined $1M" (say "ordered to pay $1M"), "95-point certification", "Korea's most-visited sites", any damages-claim amount, or A11YRepair as evidence of checker-fooling. Add no statistics or legal claims to README/UI unless the human supplies the exact sentence.
- On a non-obvious setup quirk, add one line to .agents/rules/lessons.md (create it) — not here; this file is near the 24KB rule-file limit.
