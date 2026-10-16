# Bukchon Wool — Planted Accessibility Defects Specification

## Generation provenance
- **Model**: `gemini-4-argon` (`thinking_level: low`, `max_output_tokens: 65536`)
- **Interaction ID**: `v1_ChdoNDNSYXJ6ek1ZUE5yZmNQdVppVG1BYxIXaDQzUmFyenpNWVBOcmZjUHVaaVRtQWM`
- **Timestamp (KST)**: `2026-10-16T11:34:15+09:00` → `2026-10-16T11:35:54+09:00` (`99.37s`)
- **Token usage**: `2,756` input tokens, `7,937` output tokens (`10,693` total, 0 reruns)
- **Prompt**: `STORE_PROMPT.md` (sent verbatim)

## Six planted defect types (58 violation nodes total across 4 pages)

| # | axe-core rule | Root-cause source file | Planted defect | Pages affected | Nodes |
|---|---|---|---|---|---|
| 1 | `html-has-lang` | `partials/layout-top.html` | `<html>` element has no `lang` attribute | `index.html`, `product.html`, `cart.html`, `checkout.html` | 4 |
| 2 | `button-name` | `partials/header.html` | `.search-toggle` and `.menu-toggle` icon buttons have inline SVG (`aria-hidden="true"`) and no accessible name | All 4 pages (2 per page) | 8 |
| 3 | `link-name` | `partials/header.html`, `partials/footer.html` | `.cart-link` in header and 2 `.social a` links in footer contain only `aria-hidden="true"` SVGs | All 4 pages (3 per page) | 12 |
| 4 | `label` | `partials/newsletter.html` | `#newsletter-email` input has no `<label>`, `aria-label`, `aria-labelledby`, `placeholder`, or `title` | All 4 pages (1 per page) | 4 |
| 5 | `color-contrast` | `css/tokens.css`, `css/styles.css` | Dual-context token trap: `--color-text-muted: #8C8C8C` fails 4.5:1 on `#FFFFFF` body (3.36:1) across 17 muted text nodes (`index`: 6, `product`: 5, `cart`: 3, `checkout`: 3), while passing on `#1C1F26` dark header/footer (4.90:1) | All 4 pages (6 + 5 + 3 + 3) | 17 |
| 6 | `image-alt` | `index.html`, `product.html`, `cart.html` | All 13 `<img>` elements have no `alt` attribute (`index`: banner + 6 cards = 7; `product`: main + 3 related = 4; `cart`: 2 rows = 2; `checkout`: 0) | `index.html`, `product.html`, `cart.html` | 13 |
| **Total** | | | | **4 pages** | **58** |
