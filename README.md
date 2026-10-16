# SoundCheck

**Accessibility fixes that can't fool the checker. Hear the difference.**

Planning notes and throwaway feasibility checks (no product code) were prepared before kickoff; all code and the demo site were written after 11:20 KST (first commit 11:25).

## Public URL & Replay

- **Deployed URL (Google Cloud Run, `asia-northeast3`)**: `https://soundcheck-129595935968.asia-northeast3.run.app`
- **Watch a recorded run**: Click **"Watch a recorded run"** on the landing bar or open `?replay=demo-run` to replay a recorded full-site run (including Gemini TTS audio readouts and diffs).
- **Live runs**: Live runs need a demo token to cap spend; ask at our table.

## What SoundCheck Does

AI accessibility patches can make an automated checker report zero violations while leaving the site unusable for a blind shopper—for example by setting `alt="image"` on product photos, `aria-label="button"` on icon controls, `placeholder="Email"` without a `<label>`, or `display: none` on low-contrast text.

SoundCheck closes that gap with a five-step verification loop:

1. **Scan every page**: Runs `axe-core` 4.14.0 (`wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`) inside a sandboxed browser `<iframe>` (`sandbox="allow-scripts"`) across all four pages of the demo storefront (`index.html`, `product.html`, `cart.html`, `checkout.html`).
2. **Diagnose & patch shared root causes (`gemini-4-argon`)**: Uses the Gemini Interactions API (`client.interactions.create`) with manual function calling (`update`, `submit_diagnosis`, `propose_patch`, `escalate_group`, `read_file`, `run_scan`) and implicit prefix caching to patch shared templates and CSS tokens (`partials/layout-top.html`, `partials/header.html`, `partials/newsletter.html`, `partials/footer.html`, `css/tokens.css`, `css/styles.css`). Content image `alt` text never goes through Argon: `gemini-3.8-flash` (`MODEL_ASSIST`) drafts 3 candidates per image in parallel, a human selects one, and the server writes the `alt` attribute deterministically.
3. **Re-verify the whole site (6 pass conditions + Deterministic Guard)**:
   - **Condition 1**: Targeted `(page, rule, selector)` keys eliminated across all pages.
   - **Condition 2**: Zero new violations site-wide (catches naive single-token contrast edits that fix light body text while breaking dark header/footer text).
   - **Condition 3**: Zero serious/critical violations among targeted or new keys.
   - **Condition 4**: Clean runtime (no `window.onerror`, load timeout, or unfinished `axe`).
   - **Condition 5**: Site-wide violations strictly below baseline (`axe violations 58 → M, K items need human review`).
   - **Condition 6 — Hidden "No Harm" Check** (*Patch model never saw this check*): Per page, counts of `<a>`, `<button>`, `<input>`, `<select>`, `<img>`, and visible text length must not drop, and no new `display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, `color:transparent`, `clip`, `clip-path`, or off-screen positioning rules may be added.
   - **Deterministic Guard**: Blocks generic placeholder names (`image`, `photo`, `picture`, `icon`, `button`, `link`, `img`, filenames), `role="presentation"` or empty `alt=""` on content images, form inputs named only by `placeholder` or `title`, ID mutations, and non-whitelisted files.
4. **Retry or hand off**: Passes sanitized retry feedback (remaining keys, new violations, and guard rejection reasons—never hidden check details) via `previous_interaction_id`, or escalates immediately to a human reviewer (`Escalated to human: <reason>`) on hidden-check failure or lack of progress.
5. **Approve & hear it (`gemini-3.8-flash-tts`)**: A human reviews the unified diff and human-review checklist (`axe violations N → M, K items need human review`), approves the patch, and plays the simulated screen-reader readout voiced by Gemini TTS before (`"button. link. button. image."`) and after (`"Search. Cart. Menu. Navy wool coat."`).

## On-Camera Demo Scenes

- **Scene A — Cheating patch rejected**: Applies a pre-built cheating patch (`alt="image"`, `aria-label="button"`, `aria-label="link"`, `placeholder="Email"`, and `display: none` on muted text). `axe-core` drops from `58` to `0`, while SoundCheck's Deterministic Guard (`alt="image"` is not a description) and Hidden "No Harm" Check (visible text length on `index.html` drops `936 → 811`) both reject it.
- **Scene B — Regression caught (Dual-Context Token Trap)**: Applies a naive fix (`--color-text-muted: #767676`) that reduces total violations `58 → 53` on white body cards (`4.54:1`) but introduces `12` new contrast failures on the dark `#1C1F26` header and footer (`3.63:1`). Condition 2 rejects the regression; a fresh `gemini-4-argon` run then splits `--color-text-muted` (`#595959`) and `--color-text-muted-on-dark` (`#A6ADBB`), reducing violations `45 → 28` with zero regressions (and `axe violations 58 → 0, 26 items need human review` across all six root-cause groups).
- **Scene C — Hear it**: Extracts accessible names (or role words when unnamed) in DOM order from the preview and voices Before (`"button. link. button. image."`) vs. After (`"Search. Cart. Menu. Navy wool coat."`) using `gemini-3.8-flash-tts`.
- **Scene D — Real-world template scan (`inputs/aviato/`)**: Scans 5 pages from Themefisher Aviato alongside three deterministic `"Passes axe — flagged by SoundCheck"` checks (generic `alt`/`aria-label` values, `:focus { outline: 0/none }` without replacement, and placeholder-only form inputs) and produces an Argon root-cause diagnosis card.

## Models & Environment Configuration

- `MODEL_FIX=gemini-4-argon` (`THINKING_FIX=medium`) — Root-cause diagnosis & source patching
- `MODEL_ASSIST=gemini-3.8-flash` (`THINKING_ASSIST=low`) — Parallel 3-candidate product image `alt` drafting
- `MODEL_ROUTER=gemini-3.5-flash-lite` (`THINKING_ROUTER=low`) — Additive wording review guard
- `MODEL_TTS=gemini-3.8-flash-tts` — Simulated screen-reader voice readout

## Credits & Licenses

- Manual function-calling loop adapted from the official Google Gemini Cookbook [`quickstarts/Function_calling.ipynb`](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Function_calling.ipynb) (Apache-2.0).
- In-browser accessibility engine: [`axe-core` v4.14.0](https://github.com/dequelabs/axe-core) by Deque Systems, Inc. (MPL-2.0, header preserved in `static/axe.min.js`).
- Real-input diagnostic fixture (`inputs/aviato/`): [`themefisher/aviato-bootstrap`](https://github.com/themefisher/aviato-bootstrap) `@df6686f8cd7756393d65aae3fa23b9257985765a` (MIT, `LICENSE` preserved in `inputs/aviato/LICENSE`, all `<script>` tags stripped).
- Product images generated with `gemini-nano-banana-2.1` during the hackathon.
