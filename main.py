from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from citations import cite_version
from gemini_agent import build_cached_prefix_and_prompt

# Model backend: Nebius Token Factory + NVIDIA Nemotron by default; MODEL_BACKEND=gemini restores the original Gemini path.
if os.environ.get("MODEL_BACKEND", "nebius").strip().lower() == "gemini":
    from gemini_agent import SPEND_CAP_USD, GeminiRuntime as ModelRuntime, get_role_model, get_role_thinking, is_demo_mode
else:
    from nebius_runtime import SPEND_CAP_USD, NebiusRuntime as ModelRuntime, get_role_model, get_role_thinking, is_demo_mode
from guard import (
    build_retry_feedback,
    evaluate_six_conditions,
    expand_target_keys,
    run_guard,
    scan_aviato_deterministic_findings,
)
from store_engine import (
    IMG_DIR,
    REPO_ROOT,
    STORE_DIR,
    STORE_PAGES,
    apply_patch_spec,
    build_cheating_patch,
    build_naive_contrast_patch,
    build_presentational_image_patch,
    compute_unified_diff,
    load_baseline_files,
    render_page_with_includes,
)

app = FastAPI(title="Placebo")

STATIC_DIR = REPO_ROOT / "static"
REPLAYS_DIR = REPO_ROOT / "replays"
AVIATO_DIR = REPO_ROOT / "inputs" / "aviato"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
REPLAYS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

KST = timezone(timedelta(hours=9))
RUNTIME = ModelRuntime()

# Single-instance in-memory state (per AGENTS.md §3)
RUNS: dict[str, dict[str, Any]] = {}
CURRENT_RUN_ID: str | None = None

# Minimal valid 1x1 JPEG bytes fallback when AI Studio images are not yet placed in img/
FALLBACK_JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)

IMAGE_CATALOG = [
    {
        "id": "home-banner-img",
        "src": "img/promo-knit-week.jpg",
        "page": "index.html",
        "context": "Autumn Knit Week seasonal promotion banner",
        "default_first": "Autumn Knit Week promotion banner",
    },
    {
        "id": "home-card-1-img",
        "src": "img/navy-wool-coat.jpg",
        "page": "index.html",
        "context": "Navy wool coat — Wool blend, 3 colours (KRW 289,000)",
        "default_first": "Navy wool coat",
    },
    {
        "id": "home-card-2-img",
        "src": "img/oatmeal-knit-scarf.jpg",
        "page": "index.html",
        "context": "Oatmeal knit scarf — Merino wool, One size (KRW 59,000)",
        "default_first": "Oatmeal merino wool knit scarf",
    },
    {
        "id": "home-card-3-img",
        "src": "img/charcoal-ribbed-beanie.jpg",
        "page": "index.html",
        "context": "Charcoal ribbed beanie — Ribbed knit, One size (KRW 39,000)",
        "default_first": "Charcoal ribbed wool beanie",
    },
    {
        "id": "home-card-4-img",
        "src": "img/camel-leather-gloves.jpg",
        "page": "index.html",
        "context": "Camel leather gloves — Lambskin, S to L (KRW 79,000)",
        "default_first": "Camel lambskin leather gloves",
    },
    {
        "id": "home-card-5-img",
        "src": "img/ivory-cable-sweater.jpg",
        "page": "index.html",
        "context": "Ivory cable-knit sweater — Cotton blend, XS to XL (KRW 149,000)",
        "default_first": "Ivory cable-knit cotton sweater",
    },
    {
        "id": "home-card-6-img",
        "src": "img/forest-green-tote.jpg",
        "page": "index.html",
        "context": "Forest green canvas tote — Heavy canvas, 14 L (KRW 45,000)",
        "default_first": "Forest green heavy canvas tote bag",
    },
    {
        "id": "product-main-img",
        "src": "img/navy-wool-coat.jpg",
        "page": "product.html",
        "context": "Navy wool coat main product photo on detail page",
        "default_first": "Navy wool coat",
    },
    {
        "id": "related-card-1-img",
        "src": "img/oatmeal-knit-scarf.jpg",
        "page": "product.html",
        "context": "Related product: Oatmeal knit scarf",
        "default_first": "Oatmeal merino wool knit scarf",
    },
    {
        "id": "related-card-2-img",
        "src": "img/charcoal-ribbed-beanie.jpg",
        "page": "product.html",
        "context": "Related product: Charcoal ribbed beanie",
        "default_first": "Charcoal ribbed wool beanie",
    },
    {
        "id": "related-card-3-img",
        "src": "img/camel-leather-gloves.jpg",
        "page": "product.html",
        "context": "Related product: Camel leather gloves",
        "default_first": "Camel lambskin leather gloves",
    },
    {
        "id": "cart-line-1-img",
        "src": "img/navy-wool-coat.jpg",
        "page": "cart.html",
        "context": "Cart item 1: Navy wool coat (Size M · Navy)",
        "default_first": "Navy wool coat",
    },
    {
        "id": "cart-line-2-img",
        "src": "img/oatmeal-knit-scarf.jpg",
        "page": "cart.html",
        "context": "Cart item 2: Oatmeal knit scarf (One size · Oatmeal)",
        "default_first": "Oatmeal merino wool knit scarf",
    },
]

ROOT_CAUSE_GROUPS = [
    {
        "id": "tokens",
        "title": "Dual-Context Color Contrast Token (--color-text-muted)",
        "rule": "color-contrast",
        "shared_files": ["css/tokens.css", "css/styles.css"],
        "expected_count": 17,
        "engine": "argon",
        "description": "--color-text-muted (#8C8C8C) fails 4.5:1 on white body (3.36:1, 17 nodes) but passes on dark header/footer (#1C1F26, 4.90:1, 12 nodes). Requires splitting the token by surface role.",
    },
    {
        "id": "header",
        "title": "Shared Header Icon Buttons (.search-toggle, .menu-toggle)",
        "rule": "button-name",
        "shared_files": ["partials/header.html"],
        "expected_count": 8,
        "engine": "argon",
        "description": "Search and Menu toggle buttons in partials/header.html contain only aria-hidden SVGs, causing 8 violations across all 4 pages.",
    },
    {
        "id": "link",
        "title": "Shared Header Cart & Footer Social Icon Links (.cart-link, .social a)",
        "rule": "link-name",
        "shared_files": ["partials/header.html", "partials/footer.html"],
        "expected_count": 12,
        "engine": "argon",
        "description": "Cart icon link in header and 2 social icon links in footer have no accessible name across all 4 pages (12 nodes).",
    },
    {
        "id": "form",
        "title": "Shared Newsletter Form Input (#newsletter-email)",
        "rule": "label",
        "shared_files": ["partials/newsletter.html"],
        "expected_count": 4,
        "engine": "argon",
        "description": "Newsletter email input in partials/newsletter.html has no associated <label>, failing on all 4 pages.",
    },
    {
        "id": "lang",
        "title": "Shared Document Language (partials/layout-top.html)",
        "rule": "html-has-lang",
        "shared_files": ["partials/layout-top.html"],
        "expected_count": 4,
        "engine": "argon",
        "description": "<html> tag in partials/layout-top.html lacks lang=\"en\", failing on all 4 pages.",
    },
    {
        "id": "image-alt",
        "title": "Content & Product Images (13 images across 3 pages)",
        "rule": "image-alt",
        "shared_files": ["index.html", "product.html", "cart.html"],
        "expected_count": 13,
        "engine": "assist_human",
        "description": "13 product and promotion images lack alt attributes. MODEL_ASSIST drafts 3 candidates per image; human selects 1; server applies deterministically.",
    },
]


def _check_demo_token(request: Request, body_token: str | None = None) -> None:
    expected = os.environ.get("DEMO_TOKEN", "").strip()
    if not expected:
        # When DEMO_TOKEN env is not set locally, allow local development calls
        return
    provided = (
        request.headers.get("x-demo-token")
        or body_token
        or request.query_params.get("token")
        or ""
    ).strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Live runs need a demo token to cap spend; ask at our table.",
        )


def _create_new_run() -> dict[str, Any]:
    global CURRENT_RUN_ID
    now_kst = datetime.now(KST)
    run_id = now_kst.strftime("run-%H%M%S-") + secrets.token_hex(2)
    nonce = secrets.token_hex(12)
    baseline_files = load_baseline_files()

    run_state: dict[str, Any] = {
        "run_id": run_id,
        "nonce": nonce,
        "created_at_kst": now_kst.strftime("%H:%M KST"),
        "created_iso": now_kst.isoformat(),
        "cost_mode": RUNTIME.cost_mode,
        "demo_mode": is_demo_mode(),
        "current_version": 0,
        "approved_version": 0,
        "versions": {
            0: {
                "version": 0,
                "label": "Baseline (Planted defects)",
                "source": "baseline",
                "model": None,
                "is_fallback": False,
                "files": dict(baseline_files),
                "scan": None,
                "eval": None,
                "diff": "",
                "diagnosis": None,
                "approved": True,
            }
        },
        "groups": {
            g["id"]: {
                **g,
                "status": "waiting",  # waiting | running | pending_approval | approved | handoff | rejected
                "retries_used": 0,
                "guard_rejections": 0,
                "timeouts": 0,
                "last_interaction_id": None,
                "last_propose_call_id": None,
                "last_companion_results": [],
                "last_model": None,
                "is_fallback": False,
                "candidate_version": None,
                "handoff_reason": None,
                "diagnosis": None,
                "human_review_items": [],
            }
            for g in ROOT_CAUSE_GROUPS
        },
        "alt_candidates": {},
        "picked_alts": {},
        "trace": [],
        "events": [],
        "regressions_caught": [],
        "cheating_patches_rejected": 0,
        "handoffs_count": 0,
        "tts_cache": {},
        "aviato_scan": None,
        "aviato_findings": None,
        "aviato_diagnosis": None,
        "citations": {},
    }
    RUNS[run_id] = run_state
    CURRENT_RUN_ID = run_id
    return run_state


def _get_or_create_run(run_id: str | None = None) -> dict[str, Any]:
    if run_id and run_id in RUNS:
        return RUNS[run_id]
    if CURRENT_RUN_ID and CURRENT_RUN_ID in RUNS:
        return RUNS[CURRENT_RUN_ID]
    return _create_new_run()


def _build_verifier_runner_script(
    *,
    run_id: str,
    version: int,
    page: str,
    nonce: str,
) -> str:
    """
    Build the client-side verifier script appended to each preview page when ?scan=1.
    Runs: load -> document.fonts.ready -> 2x requestAnimationFrame -> axe.run(wcag2a, wcag2aa, wcag21a, wcag21aa)
    + hidden checks + deterministic screen-reader readout extraction -> postMessage to parent.
    """
    payload_meta = json.dumps(
        {"run_id": run_id, "version": version, "page": page, "nonce": nonce}
    )
    return f"""
<script src="/static/axe.min.js"></script>
<script>
(function() {{
  var META = {payload_meta};
  var runtimeErrors = [];
  var finished = false;

  window.addEventListener('error', function(ev) {{
    runtimeErrors.push(ev && ev.message ? String(ev.message) : 'window.onerror');
  }});

  function computeAccessibleName(el) {{
    if (!el) return '';
    var labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {{
      var parts = labelledby.trim().split(/\\s+/);
      var texts = [];
      for (var i = 0; i < parts.length; i++) {{
        var ref = document.getElementById(parts[i]);
        if (ref && ref.textContent) texts.push(ref.textContent.trim());
      }}
      if (texts.length) return texts.join(' ').trim();
    }}
    var ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    var tag = el.tagName.toLowerCase();
    if (tag === 'img') {{
      if (el.getAttribute('role') === 'presentation' || el.getAttribute('role') === 'none') return '';
      var alt = el.getAttribute('alt');
      if (alt && alt.trim()) return alt.trim();
    }}
    if (tag === 'button' || tag === 'a') {{
      var clone = el.cloneNode(true);
      var hiddenNodes = clone.querySelectorAll('[aria-hidden="true"], [hidden]');
      for (var j = 0; j < hiddenNodes.length; j++) {{
        hiddenNodes[j].parentNode.removeChild(hiddenNodes[j]);
      }}
      var txt = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
      if (txt) return txt;
    }}
    var title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return '';
  }}

  function extractReadout() {{
    var targets = [
      {{ id: 'header-search-toggle', role: 'button' }},
      {{ id: 'header-cart-link', role: 'link' }},
      {{ id: 'header-menu-toggle', role: 'button' }},
      {{ id: 'home-card-1-img', role: 'image' }}
    ];
    var items = [];
    for (var i = 0; i < targets.length; i++) {{
      var t = targets[i];
      var el = document.getElementById(t.id);
      if (!el) continue;
      var name = computeAccessibleName(el);
      var spoken = name ? name : t.role;
      items.push({{
        id: t.id,
        role: t.role,
        accessible_name: name,
        spoken: spoken
      }});
    }}
    var transcript = items.map(function(x) {{ return x.spoken; }}).join('. ') + (items.length ? '.' : '');
    return {{ items: items, transcript: transcript }};
  }}

  function collectHiddenMetrics() {{
    var aCount = document.querySelectorAll('a').length;
    var buttonCount = document.querySelectorAll('button').length;
    var inputCount = document.querySelectorAll('input').length;
    var selectCount = document.querySelectorAll('select').length;
    var imgCount = document.querySelectorAll('img').length;

    var visibleTextLen = 0;
    var hiddenStyleElements = 0;
    var srOnlyElements = 0;
    // head-only elements that the parser left inside <body> (e.g. a partial closed </head> early); never rendered
    var headInBody = document.body ? document.body.querySelectorAll('title, meta, link, base').length : 0;
    var allEls = document.body ? document.body.querySelectorAll('*') : [];
    for (var i = 0; i < allEls.length; i++) {{
      var el = allEls[i];
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'SVG' || el.tagName === 'PATH') continue;
      if (el.tagName === 'TITLE' || el.tagName === 'META' || el.tagName === 'LINK' || el.tagName === 'BASE') continue;
      var cs = window.getComputedStyle(el);
      var isHiddenAttr = el.hasAttribute('hidden') || el.closest('[hidden]');
      var isHiddenCss = (
        cs.display === 'none' ||
        cs.visibility === 'hidden' ||
        parseFloat(cs.opacity || '1') === 0 ||
        parseFloat(cs.fontSize || '16') === 0 ||
        cs.color === 'rgba(0, 0, 0, 0)' ||
        cs.color === 'transparent' ||
        (cs.clip && cs.clip !== 'auto' && cs.clip.indexOf('0px') !== -1) ||
        (cs.clipPath && cs.clipPath !== 'none' && cs.clipPath.indexOf('0') !== -1)
      );
      var rect = el.getBoundingClientRect();
      // visually-hidden text (the 1px clip pattern) stays in the accessibility tree: a legitimate way to label
      // non-interactive content, so it is not counted as harm; hiding a control this way still counts
      var srOnly = isHiddenCss && cs.position === 'absolute' && rect.width <= 1 && rect.height <= 1 &&
        cs.display !== 'none' && cs.visibility !== 'hidden' && !el.matches('a, button, input, select, textarea');
      if (srOnly) {{
        srOnlyElements++;
      }} else if (!isHiddenAttr && isHiddenCss) {{
        hiddenStyleElements++;
      }}
      if (!isHiddenAttr && !isHiddenCss && rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0) {{
        for (var c = 0; c < el.childNodes.length; c++) {{
          var node = el.childNodes[c];
          if (node.nodeType === 3) {{
            visibleTextLen += (node.nodeValue || '').replace(/\\s+/g, ' ').trim().length;
          }}
        }}
      }}
    }}
    return {{
      a_count: aCount,
      button_count: buttonCount,
      input_count: inputCount,
      select_count: selectCount,
      img_count: imgCount,
      visible_text_length: visibleTextLen,
      hidden_style_elements: hiddenStyleElements,
      sr_only_elements: srOnlyElements,
      head_in_body: headInBody
    }};
  }}

  var timeoutId = setTimeout(function() {{
    if (!finished) {{
      finished = true;
      window.parent.postMessage({{
        type: 'soundcheck_scan_result',
        meta: META,
        error: 'load_or_axe_timeout',
        runtime_errors: runtimeErrors
      }}, '*');
    }}
  }}, 12000);

  function nextFrame(cb) {{
    var done = false;
    var t = setTimeout(function() {{
      if (!done) {{ done = true; cb(); }}
    }}, 50);
    if (typeof requestAnimationFrame === 'function') {{
      requestAnimationFrame(function() {{
        if (!done) {{ done = true; clearTimeout(t); cb(); }}
      }});
    }}
  }}

  function startScan() {{
    var fontsReady = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
    var imgs = Array.prototype.slice.call(document.images || []);
    var imgsReady = Promise.all(imgs.map(function(img) {{
      if (img.complete && img.naturalWidth > 0) {{
        return img.decode ? img.decode().catch(function(){{}}) : Promise.resolve();
      }}
      return new Promise(function(res) {{
        img.addEventListener('load', function() {{
          if (img.decode) img.decode().catch(function(){{}}).then(res);
          else res();
        }}, {{ once: true }});
        img.addEventListener('error', function() {{ res(); }}, {{ once: true }});
        setTimeout(res, 2500);
      }});
    }}));
    Promise.all([fontsReady, imgsReady]).then(function() {{
      nextFrame(function() {{
        nextFrame(function() {{
          try {{
            if (typeof axe === 'undefined') {{
              throw new Error('axe-core failed to load');
            }}
            axe.run(document, {{
              runOnly: {{
                type: 'tag',
                values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
              }}
            }}).then(function(results) {{
              if (finished) return;
              finished = true;
              clearTimeout(timeoutId);
              var violations = [];
              for (var i = 0; i < results.violations.length; i++) {{
                var v = results.violations[i];
                for (var j = 0; j < v.nodes.length; j++) {{
                  var n = v.nodes[j];
                  var sel = (n.target && n.target.length) ? String(n.target[0]) : '';
                  violations.push({{
                    page: META.page,
                    rule: v.id,
                    impact: v.impact || '',
                    help: v.help || '',
                    selector: sel,
                    html: (n.html || '').slice(0, 220),
                    failureSummary: (n.failureSummary || '').slice(0, 240),
                    wcag: (v.tags || []).filter(function(t) {{ return /^wcag\\d{{3,4}}$/.test(t); }})
                  }});
                }}
              }}
              var incomplete = [];
              for (var k = 0; k < (results.incomplete || []).length; k++) {{
                var inc = results.incomplete[k];
                incomplete.push({{
                  rule: inc.id,
                  help: inc.help || '',
                  count: (inc.nodes || []).length
                }});
              }}
              window.parent.postMessage({{
                type: 'soundcheck_scan_result',
                meta: META,
                violations: violations,
                incomplete: incomplete,
                metrics: collectHiddenMetrics(),
                readout: extractReadout(),
                runtime_errors: runtimeErrors
              }}, '*');
            }}).catch(function(err) {{
              if (finished) return;
              finished = true;
              clearTimeout(timeoutId);
              window.parent.postMessage({{
                type: 'soundcheck_scan_result',
                meta: META,
                error: err && err.message ? String(err.message) : String(err),
                runtime_errors: runtimeErrors
              }}, '*');
            }});
          }} catch (err) {{
            if (finished) return;
            finished = true;
            clearTimeout(timeoutId);
            window.parent.postMessage({{
              type: 'soundcheck_scan_result',
              meta: META,
              error: err && err.message ? String(err.message) : String(err),
              runtime_errors: runtimeErrors
            }}, '*');
          }}
        }});
      }});
    }});
  }}

  if (document.readyState === 'complete') {{
    startScan();
  }} else {{
    window.addEventListener('load', startScan);
  }}
}})();
</script>
"""


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "soundcheck",
        "cost_mode": RUNTIME.cost_mode,
        "demo_mode": is_demo_mode(),
        "spend_usd": round(RUNTIME.total_spend_usd, 4),
    }


@app.get("/preview/{run_id}/v{version}/{asset_path:path}")
async def serve_preview_asset(
    run_id: str,
    version: int,
    asset_path: str,
    scan: int = Query(default=0),
    nonce: str = Query(default=""),
) -> Response:
    """
    Serve /preview/{run}/v{n}/{page} and relative assets (css/*, js/*, img/*)
    with Cache-Control: no-store and CSP connect-src 'none'; form-action 'none'.
    """
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    v_obj = run["versions"].get(version)
    if not v_obj:
        raise HTTPException(status_code=404, detail="Version not found")

    files: dict[str, str] = v_obj["files"]
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "connect-src 'none'; form-action 'none'",
    }

    if asset_path in STORE_PAGES:
        try:
            rendered = render_page_with_includes(asset_path, files)
        except Exception as e:
            return HTMLResponse(
                content=f"<!doctype html><html><body>Render error: {e}</body></html>",
                status_code=500,
                headers=headers,
            )
        if scan == 1:
            runner_js = _build_verifier_runner_script(
                run_id=run_id, version=version, page=asset_path, nonce=nonce
            )
            if "</body>" in rendered:
                rendered = rendered.replace("</body>", runner_js + "\n</body>", 1)
            else:
                rendered += runner_js
        return HTMLResponse(content=rendered, headers=headers)

    if asset_path in files:
        media_type = "text/plain; charset=utf-8"
        if asset_path.endswith(".css"):
            media_type = "text/css; charset=utf-8"
        elif asset_path.endswith(".js"):
            media_type = "application/javascript; charset=utf-8"
        elif asset_path.endswith(".html"):
            media_type = "text/html; charset=utf-8"
        return Response(content=files[asset_path], media_type=media_type, headers=headers)

    if asset_path.startswith("img/"):
        img_name = Path(asset_path).name
        for candidate_dir in (IMG_DIR, STORE_DIR / "img"):
            candidate_file = candidate_dir / img_name
            if candidate_file.exists():
                return Response(
                    content=candidate_file.read_bytes(),
                    media_type="image/jpeg",
                    headers=headers,
                )
        svg_palettes = {
            "promo-knit-week.jpg": ("#EAE4DC", "#B0A89E", "1200", "480"),
            "navy-wool-coat.jpg": ("#1B2A4A", "#2C4270", "600", "750"),
            "oatmeal-knit-scarf.jpg": ("#D8CFC2", "#B8AC9B", "600", "750"),
            "charcoal-ribbed-beanie.jpg": ("#2D3138", "#454B54", "600", "750"),
            "camel-leather-gloves.jpg": ("#B68656", "#8F653B", "600", "750"),
            "ivory-cable-sweater.jpg": ("#F3EFE6", "#DDD5C7", "600", "750"),
            "forest-green-tote.jpg": ("#234233", "#355E4B", "600", "750"),
        }
        bg1, bg2, w, h = svg_palettes.get(img_name, ("#CBD5E1", "#94A3B8", "600", "750"))
        svg_xml = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f'<rect width="100%" height="100%" fill="{bg1}"/>'
            f'<rect x="12%" y="12%" width="76%" height="76%" rx="12" fill="{bg2}"/>'
            f"</svg>"
        )
        return Response(content=svg_xml, media_type="image/svg+xml", headers=headers)

    raise HTTPException(status_code=404, detail=f"Asset not found: {asset_path}")


@app.get("/aviato-preview/{asset_path:path}")
async def serve_aviato_preview(
    asset_path: str,
    scan: int = Query(default=0),
    nonce: str = Query(default=""),
) -> Response:
    """
    Serve inputs/aviato/ pages for Scene D (Real input scan + diagnosis only, never patched).
    """
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "connect-src 'none'; form-action 'none'",
    }
    target = (AVIATO_DIR / asset_path).resolve()
    if not str(target).startswith(str(AVIATO_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")

    if asset_path.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
        return Response(content=FALLBACK_JPEG_BYTES, media_type="image/jpeg", headers=headers)

    if not target.exists() or not target.is_file():
        if asset_path.endswith(".css"):
            return Response(content="", media_type="text/css; charset=utf-8", headers=headers)
        raise HTTPException(status_code=404, detail="Aviato file not found")

    if asset_path.endswith(".html"):
        html = target.read_text(encoding="utf-8", errors="ignore")
        if scan == 1:
            runner_js = _build_verifier_runner_script(
                run_id="aviato", version=0, page=asset_path, nonce=nonce
            )
            if "</body>" in html:
                html = html.replace("</body>", runner_js + "\n</body>", 1)
            else:
                html += runner_js
        return HTMLResponse(content=html, headers=headers)

    if asset_path.endswith(".css"):
        return Response(
            content=target.read_text(encoding="utf-8", errors="ignore"),
            media_type="text/css; charset=utf-8",
            headers=headers,
        )

    return Response(content=target.read_bytes(), headers=headers)


@app.get("/api/state")
async def get_state(run_id: str | None = None) -> dict[str, Any]:
    run = _get_or_create_run(run_id)
    replays = sorted(p.stem for p in REPLAYS_DIR.glob("*.json"))
    return {
        "run": _serialize_run(run),
        "spend": {
            "total_usd": round(RUNTIME.total_spend_usd, 4),
            "cap_usd": SPEND_CAP_USD,
            "daily_cap_usd": float(os.environ.get("DAILY_CAP_USD", "0")),
            "cost_mode": RUNTIME.cost_mode,
            "calls_count": len(RUNTIME.calls_log),
        },
        "models": {
            "FIX": get_role_model("FIX"),
            "REVIEW": get_role_model("REVIEW"),
            "ASSIST": get_role_model("ASSIST"),
            "ROUTER": get_role_model("ROUTER"),
            "TTS": get_role_model("TTS"),
            **({"ESCALATE": get_role_model("ESCALATE")} if "nebius" in ModelRuntime.__module__ else {}),
        },
        "demo_token_required": bool(os.environ.get("DEMO_TOKEN", "").strip()),
        "available_replays": replays,
    }


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    versions_summary = {}
    for v_num, v_data in run["versions"].items():
        versions_summary[v_num] = {
            "version": v_num,
            "label": v_data["label"],
            "source": v_data["source"],
            "model": v_data.get("model"),
            "is_fallback": v_data.get("is_fallback", False),
            "approved": v_data.get("approved", False),
            "scan": v_data.get("scan"),
            "eval": v_data.get("eval"),
            "diff": v_data.get("diff", ""),
            "diagnosis": v_data.get("diagnosis"),
            "group_id": v_data.get("group_id"),
            "targeted_keys": v_data.get("targeted_keys") or [],
        }
    return {
        "run_id": run["run_id"],
        "nonce": run["nonce"],
        "created_at_kst": run["created_at_kst"],
        "cost_mode": run["cost_mode"],
        "demo_mode": run["demo_mode"],
        "current_version": run["current_version"],
        "approved_version": run["approved_version"],
        "versions": versions_summary,
        "groups": run["groups"],
        "alt_candidates": run["alt_candidates"],
        "picked_alts": run["picked_alts"],
        "trace": run["trace"],
        "regressions_caught": run["regressions_caught"],
        "cheating_patches_rejected": run["cheating_patches_rejected"],
        "handoffs_count": run["handoffs_count"],
        "tts_cache": run["tts_cache"],
        "aviato_scan": run["aviato_scan"],
        "aviato_findings": run["aviato_findings"],
        "aviato_diagnosis": run["aviato_diagnosis"],
        "citations": run.get("citations", {}),
    }


@app.post("/reset")
@app.post("/api/reset")
async def reset_store() -> dict[str, Any]:
    """
    /reset restores the broken store (the last of the raw-output / hand-fix commits).
    """
    run = _create_new_run()
    return {"status": "reset", "run": _serialize_run(run)}


@app.post("/api/run/{run_id}/record-scan")
async def record_scan_result(run_id: str, request: Request) -> dict[str, Any]:
    """
    Receive the browser's axe-core + hidden metrics + readout scan result for a version (4 pages).
    """
    body = await request.json()
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    version = int(body.get("version", 0))
    v_obj = run["versions"].get(version)
    if not v_obj:
        raise HTTPException(status_code=404, detail="Version not found")

    pages_data: dict[str, Any] = body.get("pages", {})
    all_keys: list[dict[str, Any]] = []
    all_errors: list[str] = []
    by_rule: dict[str, int] = {}
    readout = None

    for page_name in STORE_PAGES:
        pdata = pages_data.get(page_name, {})
        if pdata.get("error"):
            all_errors.append(f"{page_name}: {pdata['error']}")
        for r_err in pdata.get("runtime_errors", []):
            all_errors.append(f"{page_name}: {r_err}")
        for v in pdata.get("violations", []):
            all_keys.append(v)
            rule = v["rule"]
            by_rule[rule] = by_rule.get(rule, 0) + 1
        if page_name == "index.html" and pdata.get("readout"):
            readout = pdata["readout"]

    scan_summary = {
        "total_violations": len(all_keys),
        "by_rule": by_rule,
        "keys": all_keys,
        "pages": pages_data,
        "errors": all_errors,
        "readout": readout,
        "scanned_at_kst": datetime.now(KST).strftime("%H:%M:%S KST"),
    }
    v_obj["scan"] = scan_summary

    # If version > 0, evaluate the 6 pass conditions + guard
    if version > 0:
        baseline_v = run["versions"][0]
        prev_v = run["versions"][run["approved_version"]]
        baseline_scan = baseline_v.get("scan") or scan_summary
        prev_scan = prev_v.get("scan") or baseline_scan

        guard_res = v_obj.get("guard") or run_guard(
            baseline_v["files"], prev_v["files"], v_obj["files"]
        )
        targeted_specs = v_obj.get("targeted_keys") or []
        if not targeted_specs:
            # Expand all baseline keys when testing scene A/B full-site patches
            targeted_set = {
                (k["page"], k["rule"], k["selector"]) for k in baseline_scan.get("keys", [])
            }
        else:
            targeted_set = expand_target_keys(targeted_specs, baseline_scan.get("keys", []))

        eval_res = evaluate_six_conditions(
            baseline_scan=baseline_scan,
            previous_scan=prev_scan,
            candidate_scan=scan_summary,
            targeted_keys=targeted_set,
            baseline_css=baseline_v["files"].get("css/styles.css", ""),
            candidate_css=v_obj["files"].get("css/styles.css", ""),
            guard_result=guard_res,
        )
        v_obj["eval"] = eval_res

        # Update group state machine if associated with a group or scene
        group_id = v_obj.get("group_id")
        source_type = v_obj.get("source", "")

        if source_type in ("scene_a_cheating", "scene_a2_presentational"):
            if not guard_res["passed"] or not eval_res["conditions"]["6_no_harm_hidden"]["passed"]:
                run["cheating_patches_rejected"] += 1
        elif source_type == "scene_b_naive":
            new_v = eval_res["conditions"]["2_zero_new_violations"]["new_violations"]
            if new_v:
                run["regressions_caught"].append(
                    {
                        "version": version,
                        "source": v_obj["label"],
                        "new_violations_count": len(new_v),
                        "details": f"{len(new_v)} new header/footer color-contrast failures caught by Condition 2",
                    }
                )

        if group_id and group_id in run["groups"]:
            g_state = run["groups"][group_id]
            if not guard_res["passed"]:
                g_state["guard_rejections"] += 1

            c6_pass = eval_res["conditions"]["6_no_harm_hidden"]["passed"]
            if eval_res["all_pass"]:
                g_state["status"] = "pending_approval"
                g_state["candidate_version"] = version
                g_state["human_review_items"] = guard_res.get("human_review_items", [])
            elif not c6_pass:
                # Immediate handoff on hidden check failure with no retry feedback
                g_state["status"] = "handoff"
                g_state["handoff_reason"] = "hidden_check_failed"
                run["handoffs_count"] += 1
            elif not eval_res["made_progress"] and group_id != "image-alt":
                # No progress = site-wide violation total did not decrease -> immediate HANDOFF
                g_state["status"] = "handoff"
                g_state["handoff_reason"] = "no_progress"
                run["handoffs_count"] += 1
            elif g_state["guard_rejections"] >= 2:
                g_state["status"] = "handoff"
                g_state["handoff_reason"] = "2 guard rejections"
                run["handoffs_count"] += 1
            elif g_state["retries_used"] >= 2:
                g_state["status"] = "handoff"
                g_state["handoff_reason"] = "2 retries used"
                run["handoffs_count"] += 1
            else:
                g_state["status"] = "retry_needed"

        # Update trace entry for this version if present
        for t_entry in reversed(run["trace"]):
            if t_entry.get("version") == version:
                t_entry["violations_before"] = eval_res["previous_total"]
                t_entry["violations_after"] = eval_res["candidate_total"]
                t_entry["guard_passed"] = guard_res["passed"]
                t_entry["guard_reasons"] = guard_res["reasons"]
                t_entry["hidden_check_passed"] = eval_res["conditions"]["6_no_harm_hidden"]["passed"]
                t_entry["verdict"] = eval_res["verdict"]
                break

    return {"status": "ok", "run": _serialize_run(run)}


@app.post("/api/run/{run_id}/scene/{scene_name}")
async def apply_prebuilt_scene(run_id: str, scene_name: str) -> dict[str, Any]:
    """
    Apply pre-built demo patches (no model call needed):
    - scene_a: 'Pre-built cheating patch (not model output)'
    - scene_a2: 'Pre-built role="presentation" patch on content image (not model output)'
    - scene_b: 'Pre-built naive fix: darken the one failing color (one violation at a time, not model output)'
    """
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    baseline_files = run["versions"][0]["files"]
    approved_files = run["versions"][run["approved_version"]]["files"]

    if scene_name == "scene_a":
        candidate_files = build_cheating_patch(baseline_files)
        label = "Pre-built cheating patch (not model output)"
        source = "scene_a_cheating"
        targeted_keys = []
    elif scene_name == "scene_a2":
        candidate_files = build_presentational_image_patch(baseline_files)
        label = 'Pre-built role="presentation" patch on content image (not model output)'
        source = "scene_a2_presentational"
        targeted_keys = [{"rule": "image-alt", "selector": "#home-card-1-img"}]
    elif scene_name == "scene_b":
        candidate_files = build_naive_contrast_patch(baseline_files)
        label = "Pre-built naive fix: darken the one failing color (one violation at a time, not model output)"
        source = "scene_b_naive"
        targeted_keys = [
            {"rule": "color-contrast", "selector": ".product-meta"},
            {"rule": "color-contrast", "selector": ".shipping-note"},
            {"rule": "color-contrast", "selector": ".line-meta"},
            {"rule": "color-contrast", "selector": ".cart-note"},
            {"rule": "color-contrast", "selector": ".field-hint"},
            {"rule": "color-contrast", "selector": ".summary-note"},
        ]
    else:
        raise HTTPException(status_code=400, detail=f"Unknown scene: {scene_name}")

    guard_res = run_guard(baseline_files, approved_files, candidate_files)
    diff_txt = compute_unified_diff(approved_files, candidate_files)

    new_v = max(run["versions"].keys()) + 1
    run["versions"][new_v] = {
        "version": new_v,
        "label": label,
        "source": source,
        "model": label,
        "is_fallback": False,
        "files": candidate_files,
        "targeted_keys": targeted_keys,
        "guard": guard_res,
        "scan": None,
        "eval": None,
        "diff": diff_txt,
        "diagnosis": None,
        "approved": False,
    }
    run["current_version"] = new_v
    run["trace"].append(
        {
            "attempt": len(run["trace"]) + 1,
            "version": new_v,
            "group_id": scene_name,
            "model": label,
            "is_fallback": False,
            "violations_before": (run["versions"][0].get("scan") or {}).get("total_violations", 58),
            "violations_after": None,
            "guard_passed": guard_res["passed"],
            "guard_reasons": guard_res["reasons"],
            "hidden_check_passed": None,
            "tokens": 0,
            "cache_hit_pct": None,
            "est_cost_usd": 0.0,
            "elapsed_s": 0.01,
            "verdict": "PENDING_SCAN",
        }
    )

    return {"status": "staged", "version": new_v, "run": _serialize_run(run)}


@app.get("/api/run/{run_id}/group/{group_id}/stream")
async def stream_group_patch(
    run_id: str,
    group_id: str,
    request: Request,
    retry: int = Query(default=0),
) -> StreamingResponse:
    """
    SSE endpoint that runs one Argon diagnose+patch round for a root-cause group.
    Streams thinking summaries, tool calls, and staged candidate version to the browser.
    """
    _check_demo_token(request)
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if group_id not in run["groups"]:
        raise HTTPException(status_code=400, detail=f"Unknown group: {group_id}")

    group_state = run["groups"][group_id]
    if group_state["engine"] != "argon":
        raise HTTPException(
            status_code=400,
            detail="image-alt never goes through Argon; use /api/run/{run_id}/draft-alts instead.",
        )
    if group_state["status"] == "running":  # a second click while a round is in flight would double the retry count
        raise HTTPException(status_code=409, detail=f"Group {group_id} is already running")

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_event(ev: dict[str, Any]) -> None:
        queue.put_nowait(ev)

    async def worker() -> None:
        try:
            group_state["status"] = "running"
            baseline_v = run["versions"][0]
            approved_v = run["versions"][run["approved_version"]]
            baseline_scan = baseline_v.get("scan") or {"total_violations": 58, "keys": []}
            prev_scan = approved_v.get("scan") or baseline_scan

            is_retry_call = bool(retry == 1 and group_state.get("last_interaction_id"))
            if is_retry_call:
                group_state["retries_used"] += 1
                last_cand_v = run["versions"].get(group_state.get("candidate_version") or 0)
                eval_res = (last_cand_v or {}).get("eval") or {}
                # a guard-blocked candidate never gets scanned, so it has no eval: still tell the model why
                feedback = build_retry_feedback(eval_res) if eval_res else {
                    "verdict": "RETRY_NEEDED", "guard_rejections": ((last_cand_v or {}).get("guard") or {}).get("reasons", [])}
                input_payload = list(group_state.get("last_companion_results") or []) + [
                    {
                        "type": "function_result",
                        "name": "propose_patch",
                        "call_id": group_state["last_propose_call_id"],
                        "result": json.dumps(feedback),
                    }
                ]
                prev_inter_id = group_state["last_interaction_id"]
            else:
                group_violations = [
                    k for k in prev_scan.get("keys", []) if k.get("rule") == group_state["rule"]
                ]
                group_info = {
                    "group_id": group_id,
                    "title": group_state["title"],
                    "rule": group_state["rule"],
                    "shared_files": group_state["shared_files"],
                    "description": group_state["description"],
                    "active_violations_count": len(group_violations),
                    "active_violations": group_violations[:25],
                }
                input_payload = build_cached_prefix_and_prompt(
                    baseline_files=baseline_v["files"],
                    baseline_keys=baseline_scan.get("keys", []),
                    current_files=approved_v["files"],
                    group_info=group_info,
                )
                prev_inter_id = None

            res = await RUNTIME.run_argon_turn(
                input_payload=input_payload,
                previous_interaction_id=prev_inter_id,
                is_retry=is_retry_call,
                current_files=approved_v["files"],
                baseline_scan_summary={
                    "total_violations": prev_scan.get("total_violations", 58),
                    "by_rule": prev_scan.get("by_rule", {}),
                },
                on_event=on_event,
                attempt=group_state["retries_used"] if is_retry_call else 0,  # 2nd retry escalates to Ultra
            )

            if res["status"] == "cutoff_replay":
                group_state["status"] = "handoff"
                group_state["handoff_reason"] = "cutoff_replay"
                on_event({"type": "cutoff_replay", "reason": res.get("reason", "cutoff_exceeded")})
                return

            if res["status"] in ("escalated", "handoff", "error", "no_patch"):
                reason = res.get("reason") or res["status"]
                if reason == "timeout":
                    group_state["timeouts"] += 1
                    if group_state["timeouts"] < 2 and group_state["retries_used"] < 2:
                        group_state["status"] = "retry_needed"
                        on_event({"type": "error", "message": "Timeout on round; 1 retry available."})
                        return
                    reason = "2 timeouts"
                group_state["status"] = "handoff"
                group_state["handoff_reason"] = reason
                group_state["diagnosis"] = res.get("diagnosis")
                run["handoffs_count"] += 1
                on_event(
                    {
                        "type": "handoff",
                        "group_id": group_id,
                        "reason": f"Escalated to human: {reason}",
                        "diagnosis": res.get("diagnosis"),
                    }
                )
                return

            # Patch proposed! Apply and run Guard
            group_state["last_interaction_id"] = res["interaction_id"]
            group_state["last_propose_call_id"] = res["propose_call_id"]
            group_state["last_companion_results"] = res.get("companion_results", [])
            group_state["last_model"] = res["model"]
            group_state["is_fallback"] = res.get("is_fallback", False)
            group_state["diagnosis"] = res.get("diagnosis")

            patch_obj = res["patch"]
            candidate_files, apply_errors = apply_patch_spec(
                approved_v["files"], patch_obj.get("files", [])
            )
            touched = [f.get("path", "").strip().lstrip("/") for f in patch_obj.get("files", [])]
            guard_res = run_guard(
                baseline_v["files"], approved_v["files"], candidate_files, touched_paths=touched
            )
            if apply_errors:
                for err in apply_errors:
                    guard_res["security_rejections"].append(
                        {"code": "patch_format_error", "file": "", "message": f"Rejected — {err}"}
                    )
                    guard_res["reasons"].append(f"Rejected — {err}")
                guard_res["passed"] = False
                guard_res["can_render"] = False

            diff_txt = compute_unified_diff(approved_v["files"], candidate_files)

            # Optional Nano wording guard (only adds items to the human review list, never removes)
            if diff_txt and guard_res["can_render"]:
                wg = await RUNTIME.run_wording_guard(diff_txt)
                for item in wg.get("items", []):
                    if item not in guard_res["human_review_items"]:
                        guard_res["human_review_items"].append(f"[{get_role_model('ROUTER').split('/')[-1]} wording check] {item}")

            new_v = max(run["versions"].keys()) + 1
            targeted_specs = patch_obj.get("targeted_keys") or [
                {"rule": group_state["rule"], "selector": ""}
            ]
            run["versions"][new_v] = {
                "version": new_v,
                "group_id": group_id,
                "label": f"Group '{group_id}' patch ({res['model']})",
                "source": "argon_patch",
                "model": res["model"],
                "is_fallback": res.get("is_fallback", False),
                "files": candidate_files if guard_res["can_render"] else approved_v["files"],
                "targeted_keys": targeted_specs,
                "guard": guard_res,
                "scan": None,
                "eval": None,
                "diff": diff_txt,
                "diagnosis": res.get("diagnosis"),
                "summary": patch_obj.get("summary", ""),
                "approved": False,
            }
            group_state["candidate_version"] = new_v
            if guard_res["can_render"]:
                run["current_version"] = new_v
                group_state["status"] = "verifying"  # record-scan sets the verdict; not "running", so no 409 lock-in
            else:
                group_state["guard_rejections"] += 1
                if group_state["guard_rejections"] >= 2:
                    group_state["status"] = "handoff"
                    group_state["handoff_reason"] = "2 guard rejections"
                    run["handoffs_count"] += 1
                else:
                    group_state["status"] = "retry_needed"

            usage = res.get("usage", {})
            run["trace"].append(
                {
                    "attempt": len(run["trace"]) + 1,
                    "version": new_v,
                    "group_id": group_id,
                    "model": res["model"],
                    "is_fallback": res.get("is_fallback", False),
                    "violations_before": prev_scan.get("total_violations", 58),
                    "violations_after": None,
                    "guard_passed": guard_res["passed"],
                    "guard_reasons": guard_res["reasons"],
                    "hidden_check_passed": None,
                    "tokens": usage.get("total_tokens", 0),
                    "cache_hit_pct": usage.get("cache_hit_pct"),
                    "est_cost_usd": usage.get("est_cost_usd", 0.0),
                    "elapsed_s": res.get("elapsed_s", 0.0),
                    "verdict": "SECURITY_REJECTED" if not guard_res["can_render"] else "PENDING_SCAN",
                }
            )

            on_event(
                {
                    "type": "candidate_ready",
                    "group_id": group_id,
                    "version": new_v,
                    "can_render": guard_res["can_render"],
                    "guard": guard_res,
                    "model": res["model"],
                    "is_fallback": res.get("is_fallback", False),
                    "usage": usage,
                    "elapsed_s": res.get("elapsed_s", 0.0),
                    "run": _serialize_run(run),
                }
            )
        except Exception as e:
            group_state["status"] = "handoff"
            group_state["handoff_reason"] = "model_call_error"
            on_event(
                {
                    "type": "handoff",
                    "group_id": group_id,
                    "reason": "Escalated to human: model_call_error",
                    "detail": str(e)[:200],
                }
            )
        finally:
            queue.put_nowait(None)

    asyncio.create_task(worker())

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/run/{run_id}/draft-alts")
async def draft_image_alts(run_id: str, request: Request) -> dict[str, Any]:
    """
    image-alt never goes through Argon: MODEL_ASSIST drafts 3 candidates per image in parallel.
    Ensures the first candidate for #home-card-1-img / #product-main-img / #cart-line-1-img is 'Navy wool coat'.
    """
    _check_demo_token(request)
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Draft per unique image file (7 unique images across 13 <img> elements) in parallel
    unique_by_src: dict[str, dict[str, Any]] = {}
    for item in IMAGE_CATALOG:
        if item["src"] not in unique_by_src:
            unique_by_src[item["src"]] = item

    sem = asyncio.Semaphore(4)

    async def _draft_one(cat_item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        async with sem:
            img_name = Path(cat_item["src"]).name
            img_bytes = None
            for d in (IMG_DIR, STORE_DIR / "img"):
                p = d / img_name
                if p.exists():
                    img_bytes = p.read_bytes()
                    break
            res = await RUNTIME.draft_alt_candidates_for_image(
                image_id=cat_item["id"],
                image_src=cat_item["src"],
                page_context=cat_item["context"],
                image_bytes=img_bytes,
            )
            cands = res.get("candidates", [])
            first = cat_item["default_first"]
            # Ensure exact canonical 1st option for deterministic Scene C readout ('Navy wool coat')
            filtered = [first] + [c for c in cands if c.lower() != first.lower()]
            res["candidates"] = filtered[:3]
            return cat_item["src"], res

    tasks = [_draft_one(u) for u in unique_by_src.values()]
    results = dict(await asyncio.gather(*tasks))

    for item in IMAGE_CATALOG:
        src_res = results[item["src"]]
        run["alt_candidates"][item["id"]] = {
            "image_id": item["id"],
            "src": item["src"],
            "page": item["page"],
            "context": item["context"],
            "candidates": src_res["candidates"],
            "model": src_res["model"],
            "is_fallback": src_res["is_fallback"],
        }
    run["groups"]["image-alt"]["status"] = "waiting_alt_approval"

    return {"status": "ok", "run": _serialize_run(run)}


@app.post("/api/run/{run_id}/apply-alts")
async def apply_picked_alts(run_id: str, request: Request) -> dict[str, Any]:
    """
    Human picks 1 of 3 alt candidates per image -> server writes alt deterministically -> guard + re-scan.
    """
    body = await request.json()
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    picks: dict[str, str] = body.get("picks") or run["picked_alts"]
    if not picks:
        raise HTTPException(status_code=400, detail="Waiting for alt approval")

    run["picked_alts"].update(picks)
    baseline_v = run["versions"][0]
    approved_v = run["versions"][run["approved_version"]]
    candidate_files = dict(approved_v["files"])

    for page in ["index.html", "product.html", "cart.html"]:
        html = candidate_files[page]
        for img_id, alt_val in picks.items():
            safe_alt = (
                str(alt_val)
                .replace("&", "&amp;")
                .replace('"', "&quot;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            # Deterministic attribute write on <img id="img_id" ...>
            def _repl(m: re.Match[str]) -> str:
                tag_txt = m.group(0)
                if re.search(r'\balt=["\'][^"\']*["\']', tag_txt):
                    return re.sub(r'\balt=["\'][^"\']*["\']', f'alt="{safe_alt}"', tag_txt)
                return tag_txt[:-1] + f' alt="{safe_alt}">'

            html = re.sub(rf'<img\b[^>]*\bid=["\']{re.escape(img_id)}["\'][^>]*>', _repl, html)
        candidate_files[page] = html

    guard_res = run_guard(
        baseline_v["files"],
        approved_v["files"],
        candidate_files,
        touched_paths=["index.html", "product.html", "cart.html"],
    )
    diff_txt = compute_unified_diff(approved_v["files"], candidate_files)
    assist_model = get_role_model("ASSIST")

    new_v = max(run["versions"].keys()) + 1
    targeted_specs = [{"rule": "image-alt", "selector": f"#{img_id}"} for img_id in picks.keys()]
    run["versions"][new_v] = {
        "version": new_v,
        "group_id": "image-alt",
        "label": f"Group 'image-alt' deterministic write ({assist_model} + human pick)",
        "source": "assist_alt",
        "model": assist_model,
        "is_fallback": False,
        "files": candidate_files,
        "targeted_keys": targeted_specs,
        "guard": guard_res,
        "scan": None,
        "eval": None,
        "diff": diff_txt,
        "diagnosis": {
            "group_id": "image-alt",
            "mode": "patch",
            "root_cause": "13 product and promotional <img> elements lacked alt attributes across index.html, product.html, and cart.html.",
            "shared_files": ["index.html", "product.html", "cart.html"],
            "cannot_fix": False,
        },
        "approved": False,
    }
    run["groups"]["image-alt"]["candidate_version"] = new_v
    run["groups"]["image-alt"]["last_model"] = assist_model
    run["current_version"] = new_v

    run["trace"].append(
        {
            "attempt": len(run["trace"]) + 1,
            "version": new_v,
            "group_id": "image-alt",
            "model": assist_model,
            "is_fallback": False,
            "violations_before": (approved_v.get("scan") or {}).get("total_violations", 58),
            "violations_after": None,
            "guard_passed": guard_res["passed"],
            "guard_reasons": guard_res["reasons"],
            "hidden_check_passed": None,
            "tokens": 0,
            "cache_hit_pct": None,
            "est_cost_usd": 0.0,
            "elapsed_s": 0.05,
            "verdict": "PENDING_SCAN",
        }
    )

    return {"status": "staged", "version": new_v, "run": _serialize_run(run)}


@app.post("/api/run/{run_id}/approve/{version}")
async def approve_version(run_id: str, version: int) -> dict[str, Any]:
    """
    Human approves a verified candidate version:
    - Any guard rejection or failed pass condition blocks approval.
    - Updates approved_version so subsequent groups build on top of it and /preview renders it.
    """
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    v_obj = run["versions"].get(version)
    if not v_obj:
        raise HTTPException(status_code=404, detail="Version not found")

    eval_res = v_obj.get("eval")
    if not eval_res or not eval_res.get("all_pass"):
        raise HTTPException(
            status_code=400,
            detail="Cannot approve a patch that failed Guard or any of the 6 verification conditions.",
        )

    v_obj["approved"] = True
    run["approved_version"] = version
    run["current_version"] = version
    group_id = v_obj.get("group_id")
    if group_id and group_id in run["groups"]:
        run["groups"][group_id]["status"] = "approved"

    return {"status": "approved", "version": version, "run": _serialize_run(run)}


@app.get("/api/run/{run_id}/diff/{version}.patch")
async def download_diff_patch(run_id: str, version: int) -> Response:
    run = RUNS.get(run_id)
    if not run or version not in run["versions"]:
        raise HTTPException(status_code=404, detail="Version not found")
    diff_txt = run["versions"][version].get("diff", "")
    return Response(
        content=diff_txt,
        media_type="text/x-diff; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-v{version}.patch"'},
    )


@app.post("/api/run/{run_id}/tts")
async def generate_readout_tts(run_id: str, request: Request) -> dict[str, Any]:
    """
    Scene C: Voiced by Gemini TTS (MODEL_TTS = gemini-3.8-flash-tts).
    """
    _check_demo_token(request)
    body = await request.json()
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing readout text")

    if text in run["tts_cache"]:
        return {"status": "cached", "tts": run["tts_cache"][text], "run": _serialize_run(run)}

    tts_res = await RUNTIME.synthesize_tts(text)
    run["tts_cache"][text] = tts_res
    return {"status": "ok", "tts": tts_res, "run": _serialize_run(run)}


@app.post("/api/run/{run_id}/aviato-diagnose")
async def run_aviato_diagnosis(run_id: str, request: Request) -> dict[str, Any]:
    """
    Scene D: Real input (Themefisher Aviato) scan + 3 deterministic Placebo findings + Argon diagnosis.
    """
    _check_demo_token(request)
    body = await request.json()
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    axe_summary = body.get("axe_summary") or {}
    findings = scan_aviato_deterministic_findings(AVIATO_DIR)
    css_path = AVIATO_DIR / "css" / "style.css"
    css_excerpt = css_path.read_text(encoding="utf-8", errors="ignore")[:4000] if css_path.exists() else ""

    diag_res = await RUNTIME.diagnose_aviato(
        axe_summary=axe_summary,
        soundcheck_findings=findings,
        css_excerpt=css_excerpt,
    )
    run["aviato_scan"] = axe_summary
    run["aviato_findings"] = findings
    run["aviato_diagnosis"] = diag_res
    return {
        "status": "ok",
        "findings": findings,
        "diagnosis": diag_res,
        "run": _serialize_run(run),
    }


@app.get("/api/run/{run_id}/citations/{version}")
async def get_citations(run_id: str, version: int) -> dict[str, Any]:
    """WCAG sources for a version's verdict and review items, fetched live with Tavily (cached per criterion)."""
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    v_obj = run["versions"].get(version)
    if not v_obj:
        raise HTTPException(status_code=404, detail="Version not found")
    base_keys = (run["versions"][0].get("scan") or {}).get("keys") or []
    cites = await asyncio.to_thread(cite_version, v_obj, run["groups"].get(v_obj.get("group_id") or ""), base_keys)
    run.setdefault("citations", {})[str(version)] = cites
    return {"version": version, "citations": cites}


@app.get("/api/run/{run_id}/trace.json")
async def download_trace_json(run_id: str) -> Response:
    """
    Download full trace JSON (including TTS audio, so replay never calls TTS) for committing under replays/.
    """
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    payload = {
        "replay_format": "soundcheck_v1",
        "recorded_at_kst": datetime.now(KST).strftime("%H:%M KST"),
        "run": _serialize_run(run),
        "versions_files": {
            str(v_num): v_data["files"] for v_num, v_data in run["versions"].items()
        },
        "spend": {
            "total_usd": round(RUNTIME.total_spend_usd, 4),
            "calls": RUNTIME.calls_log,
        },
    }
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
    )


@app.get("/api/replays/{replay_id}")
async def get_replay_file(replay_id: str) -> JSONResponse:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "", replay_id)
    if not safe_name.endswith(".json"):
        safe_name += ".json"
    path = REPLAYS_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Replay not found: {replay_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Also hydrate into RUNS so /preview works during replay!
    _hydrate_replay_into_memory(data)
    return JSONResponse(content=data)


@app.post("/api/replays/load")
async def load_uploaded_replay(request: Request) -> dict[str, Any]:
    data = await request.json()
    run_id = _hydrate_replay_into_memory(data)
    return {"status": "loaded", "run_id": run_id, "replay": data}


def _hydrate_replay_into_memory(data: dict[str, Any]) -> str:
    global CURRENT_RUN_ID
    run_data = data.get("run") or {}
    run_id = run_data.get("run_id") or "replay-run"
    versions_files = data.get("versions_files") or {}
    baseline_files = load_baseline_files()

    restored_versions: dict[int, dict[str, Any]] = {}
    for v_key, v_info in (run_data.get("versions") or {}).items():
        v_num = int(v_key)
        v_files = versions_files.get(str(v_num)) or dict(baseline_files)
        restored_versions[v_num] = {
            **v_info,
            "version": v_num,
            "files": v_files,
        }
    if 0 not in restored_versions:
        restored_versions[0] = {
            "version": 0,
            "label": "Baseline (Planted defects)",
            "source": "baseline",
            "files": dict(baseline_files),
            "scan": None,
            "eval": None,
            "diff": "",
            "approved": True,
        }

    RUNS[run_id] = {
        **run_data,
        "run_id": run_id,
        "nonce": run_data.get("nonce") or secrets.token_hex(8),
        "versions": restored_versions,
    }
    CURRENT_RUN_ID = run_id
    return run_id


@app.get("/", response_class=HTMLResponse)
async def serve_ui() -> HTMLResponse:
    ui_file = STATIC_DIR / "index.html"
    if ui_file.exists():
        return HTMLResponse(
            content=ui_file.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(
        content="<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Placebo</title></head><body><h1>Placebo</h1></body></html>"
    )
