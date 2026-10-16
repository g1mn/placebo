# Placebo — Hackathon Submission Copy & Video Script

**Tagline**: Accessibility fixes that can't fool the checker. Hear the difference.

---

## 1. Project Description

### Short Version (~100 Words)

Placebo is an accessibility repair workflow that stops AI patches from gaming automated checkers. Code can pass an automated checker while failing blind shoppers: on our four-page demo storefront, a cheating patch drives `axe-core` violations from 58 down to 0 yet labels every photo `alt="image"` and hides 125 characters of visible text (`936 → 811`), while a naive contrast edit lowers violations from 58 to 53 but introduces 12 new failures on dark surfaces. Placebo blocks both. `gemini-4-argon` patches shared templates and CSS tokens at the source, six whole-site conditions verify every change, a human approves each diff, and `gemini-3.8-flash-tts` voices the screen-reader readout before (`"button. link. button. image."`) and after (`"Search. Cart. Menu. Navy wool coat."`). Final result: axe violations `58 → 0, 26 items need human review`.

---

### Full Version

#### One-Line Summary
**Placebo** repairs web accessibility defects at the shared template and CSS token level with `gemini-4-argon`, blocks patches that fool `axe-core` or cause cross-page regressions, and lets developers hear the screen-reader difference before and after.

#### Measured Results (From Recorded & Deployed Runs)
- **Baseline scan (`v0`)**: `58` `axe-core` violations across 4 storefront pages (`index.html`, `product.html`, `cart.html`, `checkout.html`) — `button-name`: 8, `color-contrast`: 17, `html-has-lang`: 4, `image-alt`: 13, `label`: 4, `link-name`: 12.
- **Scene A — Cheating patch rejected (`v1`)**: A pre-built cheating patch scores `0` `axe-core` violations, but Placebo rejects it via its Deterministic Guard (`alt="image"` is not a description; `aria-label="button"` and `aria-label="link"` are generic role words; `#newsletter-email` relies only on `placeholder`) and Condition 6, the hidden "No Harm" check the patch model never sees (visible text length on `index.html` drops from `936` to `811` characters due to `display: none`).
- **Scene B — Cross-page regression caught (`v2`)**: A naive per-violation edit (`--color-text-muted: #767676`) reduces total violations from `58` to `53`, but Condition 2 catches `12` newly introduced serious contrast failures on dark `#1C1F26` header and footer surfaces across all 4 pages and rejects the patch.
- **Root-cause remediation (`v3` → `v8`)**:
  - `v3` (`image-alt`, `gemini-3.8-flash` parallel 3-candidate drafting from product images + human selection + deterministic attribute write): `58 → 45`
  - `v4` (`tokens`, `gemini-4-argon` surface-role token split: `--color-text-muted: #595959` for light surfaces and `--color-text-muted-on-dark: #A6ADBB` for dark header/footer): `45 → 28`
  - `v5` (`header`, `gemini-4-argon` shared `partials/header.html`): `28 → 20`
  - `v6` (`link`, `gemini-4-argon` shared `partials/header.html` + `partials/footer.html`): `20 → 8`
  - `v7` (`form`, `gemini-4-argon` shared `partials/newsletter.html`): `8 → 4`
  - `v8` (`lang`, `gemini-4-argon` shared `partials/layout-top.html`): `4 → 0`
- **Final verified outcome**: `axe violations 58 → 0, 26 items need human review` (15 alt-text/wording checks + 11 semantic label reviews flagged by `gemini-3.5-flash-lite` and the deterministic guard).
- **Live Cloud Run latency**: Streamed `gemini-4-argon` diagnose-and-patch rounds completed in `13.9 s` and `16.2 s` on the deployed Cloud Run service (`asia-northeast3`).
- **Audible readout (`gemini-3.8-flash-tts`)**: Before: `"button. link. button. image."` → After: `"Search. Cart. Menu. Navy wool coat."`
- **Real-world input (`inputs/aviato/`)**: Scanned 5 pages of the MIT-licensed Themefisher Aviato e-commerce template and surfaced 3 patterns that pass `axe-core` yet fail users (`alt="image"` / generic labels, `:focus { outline: 0 }` without replacement focus styles, and placeholder-only inputs), paired with an Argon root-cause diagnosis.

#### Target Users
Blind and low-vision shoppers who rely on screen readers and keyboard navigation to browse and purchase from online stores, and the frontend engineers, small e-commerce teams, and design-system maintainers who use Placebo to repair template-level accessibility bugs without shipping superficial attributes that fail real screen-reader users.

#### How It Works in Three Steps
1. **Whole-Site Sandboxed Scan**: Loads all 4 store pages inside a sandboxed browser `<iframe>` (`sandbox="allow-scripts"`) with `CSP: connect-src 'none'; form-action 'none'`, runs `axe-core` 4.14.0 (`wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`), records DOM element counts and visible text length, and deterministically extracts the top-of-page accessible name readout.
2. **Root-Cause Diagnosis & Source Patching**:
   - Shared template and token defects (`tokens`, `header`, `link`, `form`, `lang`) are diagnosed and patched at the source by `gemini-4-argon` via the Gemini Interactions API (`client.interactions.create`) using manual function calling (`update`, `submit_diagnosis`, `propose_patch`, `escalate_group`, `read_file`, `run_scan`).
   - Product and banner image descriptions (`image-alt`) never go through Argon: `gemini-3.8-flash` (`MODEL_ASSIST`) inspects each image and drafts 3 candidate descriptions in parallel, a human selects one, and the server writes the `alt` attribute deterministically.
3. **Six-Condition Re-Verification, Human Approval & Voice Readout**:
   - Every candidate patch is re-scanned across all 4 pages and must pass 6 deterministic conditions: (1) targeted `(page, rule, selector)` keys eliminated, (2) zero new violations site-wide, (3) zero serious/critical among the targeted keys and any new keys, (4) clean runtime (`window.onerror` free), (5) total violations strictly below baseline, and (6) the hidden "No Harm" check (no drop in `<a>`, `<button>`, `<input>`, `<select>`, `<img>` counts or visible text length, and no newly added `display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, `color:transparent`, `clip`, or `clip-path` rules).
   - `gemini-3.5-flash-lite` (`MODEL_ROUTER`) adds wording review items for human inspection. Once a human approves the unified diff, `gemini-3.8-flash-tts` (`MODEL_TTS`) voices the deterministic screen-reader readout before and after.

#### Why `gemini-4-argon`
Single-node accessibility fixes fail when a single CSS custom property (`--color-text-muted`) is shared across light product cards (`#FFFFFF` / `#FAF8F5`) and dark chrome (`#1C1F26`). Darkening the token to `#767676` satisfies contrast on white cards (`4.54:1`) while breaking 12 elements in the dark header and footer (`3.63:1`). `gemini-4-argon` reasons across all 4 pages' violation tables, `css/tokens.css`, `css/styles.css`, and shared HTML partials simultaneously, splitting the token by surface role (`--color-text-muted: #595959` and `--color-text-muted-on-dark: #A6ADBB`) and eliminating all 17 contrast violations with zero regressions.

#### Limitations
- Evaluates static HTML templates and CSS files; dynamic client-side state transitions, custom ARIA widget keyboard flows, and full manual screen-reader usability testing still require human verification.
- Passing automated scans and Placebo's guards reduces barrier density (`axe violations 58 → 0, 26 items need human review`) but never replaces human review or real-user testing; product image `alt` text and visible copy changes always require explicit human approval.

---

## 2. Three-Minute Demo Video Script (Narration & Subtitles)

| Time | Scene & Visual on Screen | Narration & Burnt-in Subtitle (English) |
|---|---|---|
| **0:00–0:08** | **Hook — Before Readout**: Storefront preview (`v0 Baseline`, 58 violations) + Scene C `Play Before (Gemini TTS)` active. | *(Gemini TTS Readout)*: `"button. link. button. image."`<br>*(Subtitle)*: What a blind shopper hears at the top of this store: `"button. link. button. image."` |
| **0:08–0:17** | **Hook — Supreme Court Ruling**: Context card over storefront preview. | `"In March 2026, Korea's Supreme Court ruled that online marketplaces must provide alt text that blind shoppers can read with a screen reader."` |
| **0:17–0:25** | **Hook — Cheating Patch Rejected**: `v1 Scene A (Cheating)` selected; `axe-core` box shows `58 → 0` (green), while `Deterministic Guard` (`REJECTED — alt="image" is not a description`) and `Condition 6: No Harm` (`FAILED`) turn red. | `"Yet an AI accessibility patch can score zero violations in an automated checker and still leave the page unusable."` |
| **0:25–0:35** | **Hook — After Readout & Title**: `v8` selected (`axe violations 58 → 0, 26 items need human review`) + Scene C `Play After (Gemini TTS)` active. | *(Gemini TTS Readout)*: `"Search. Cart. Menu. Navy wool coat."`<br>`"Placebo catches fixes that fool the checker. Hear the difference."` |
| **0:35–0:52** | **Problem**: Two evidence cards on screen (GitHub Accessibility Team warning + 24 improvements vs. 20 regressions finding). | `"GitHub's accessibility team warns that code can pass automated checks and still be unusable in practice. And research on unverified AI accessibility edits found 20 regressions for every 24 improvements — nearly one step back for every step forward."` |
| **0:52–1:12** | **Scene D — Real Input (`inputs/aviato/`)**: Scene D panel showing the 5-page Aviato scan, the 3 `"Passes axe — flagged by Placebo"` badges, and the Argon root-cause diagnosis card. | `"Look at this real open-source store template, Aviato. Placebo scans five pages and flags three patterns that axe-core passes: generic alt='image' attributes on product photos, focus outlines stripped in CSS, and form inputs named only by a placeholder."` |
| **1:12–1:32** | **Baseline (`v0`) & Root-Cause Groups**: `v0 Baseline (58)` across 4 pages (`button-name: 8, color-contrast: 17, html-has-lang: 4, image-alt: 13, label: 4, link-name: 12`) and the 6 shared root-cause groups. | `"On our four-page demo store with 58 planted violations, Gemini 4 Argon inspects the shared HTML partials, CSS tokens, and whole-site scan results to patch each root cause at the source — reaching a candidate patch in 13.9 to 16.2 seconds on Cloud Run."` |
| **1:32–1:55** | **Scene B — Regression Caught (`v2`)**: `v2 Scene B (Naive #767676)` selected; `axe-core` box turns red (`58 → 53 · 12 new header/footer failures!`) and Condition 2 fails. | `"Watch what happens with a naive per-violation fix that darkens the muted gray token to #767676. Total violations drop from 58 to 53, but it creates 12 new contrast failures on the dark header and footer. Placebo re-scans all four pages and Condition 2 rejects the regression."` |
| **1:55–2:12** | **Argon Token Role Split (`v4`)**: `v4 tokens (28) ✅` selected; unified diff shows `--color-text-muted: #595959` and `--color-text-muted-on-dark: #A6ADBB`; all 6 conditions green `✅`. | `"In a fresh run, Gemini 4 Argon splits the token by surface role: #595959 for light backgrounds and #A6ADBB for dark chrome. Violations drop from 45 to 28 across all four pages with zero new failures."` |
| **2:12–2:28** | **Scene A — Cheating Patch Rejected (`v1`)**: `v1 Scene A (Cheating)` rejection details highlighted (`alt="image"`, `aria-label="button"`, and Condition 6 visible text drop `936 → 811`). | `"And when a cheating patch tries to game the checker, axe-core reports zero violations. Placebo rejects it immediately: the deterministic guard blocks alt='image' and generic role labels, and a hidden no-harm check that the patching model never sees catches 125 characters of visible text disappearing from 936 down to 811."` |
| **2:28–2:45** | **Human Approval & Before/After Readout (`v3` & `v8`)**: `Pick 1 of 3 Alt Candidates (gemini-3.8-flash)` panel → `Approve & Update /preview` → Scene C Before and After audio playback. | `"Image alt text never goes through Argon: Gemini 3.8 Flash drafts three candidates from each product photo, a human picks one, and every diff requires human approval. Hear the top of the store before: 'button. link. button. image.' And after: 'Search. Cart. Menu. Navy wool coat.'"` |
| **2:45–3:00** | **Results Table (`v8`) & Models**: Run Summary & Results Table (`axe violations 58 → 0, 26 items need human review`, `Regressions caught: 1`, `Cheating patches rejected: 1`). | `"Across the full run, axe violations drop from 58 to 0, while 26 items are handed to a human reviewer on purpose. Built with Gemini 4 Argon, Gemini 3.8 Flash, Gemini 3.5 Flash-Lite, Gemini 3.8 Flash TTS, and Nano Banana."` |

---

## 3. `README.md` Verification Checklist (Only What Actually Ran)

- [x] **Disclosure line verbatim**: `"Planning notes and throwaway feasibility checks (no product code) were prepared before kickoff; all code and the demo site were written after 11:20 KST (first commit 11:25)."`
- [x] **Public Cloud Run URL & Replay**: `https://soundcheck-129595935968.asia-northeast3.run.app` and `?replay=demo-run` (`replays/demo-run.json` committed and verified HTTP 200 without authentication).
- [x] **Demo token gate**: Live endpoints require `X-Demo-Token` (`403` without token: `"Live runs need a demo token to cap spend; ask at our table."`).
- [x] **Real measured numbers only**: Baseline `58` (`button-name` 8, `color-contrast` 17, `html-has-lang` 4, `image-alt` 13, `label` 4, `link-name` 12) → `0` violations, `26` items need human review; Scene A `0` axe violations rejected by Guard and Condition 6 (`936 → 811` visible text length); Scene B `58 → 53` rejected by Condition 2 (`12` new header/footer contrast failures); Token split `#595959` / `#A6ADBB` (`45 → 28`); Readout `"button. link. button. image."` → `"Search. Cart. Menu. Navy wool coat."`.
- [x] **Credits & Licenses**: Gemini Cookbook `Function_calling.ipynb` (Apache-2.0), `axe-core` v4.14.0 (MPL-2.0), `themefisher/aviato-bootstrap` `@df6686f` (MIT), and `"Product images generated with gemini-nano-banana-2.1 during the hackathon."`
- [x] **Zero banned phrases**: Verified 0 hits for prohibited compliance/certification/legal claim phrases in `README.md`, `SUBMISSION.md`, and `static/index.html`.
