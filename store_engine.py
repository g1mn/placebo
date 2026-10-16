from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
STORE_DIR = REPO_ROOT / "store"
IMG_DIR = REPO_ROOT / "img"

STORE_PAGES = ["index.html", "product.html", "cart.html", "checkout.html"]

WHITELIST_HTML_CSS = {
    "index.html",
    "product.html",
    "cart.html",
    "checkout.html",
    "partials/layout-top.html",
    "partials/header.html",
    "partials/newsletter.html",
    "partials/footer.html",
    "css/tokens.css",
    "css/styles.css",
}

ALL_STORE_FILES = sorted(WHITELIST_HTML_CSS | {"js/main.js"})

INCLUDE_LINE_RE = re.compile(r"^<!-- include: ([a-zA-Z0-9_.-]+) -->$")


def load_baseline_files() -> dict[str, str]:
    """Load the clean baseline store files from disk."""
    files: dict[str, str] = {}
    for rel_path in ALL_STORE_FILES:
        full_path = STORE_DIR / rel_path
        if full_path.exists():
            files[rel_path] = full_path.read_text(encoding="utf-8")
    return files


def render_page_with_includes(page_name: str, files: dict[str, str]) -> str:
    """Render a top-level store HTML page by expanding single-level whole-line include markers."""
    if page_name not in STORE_PAGES:
        raise ValueError(f"Unknown store page: {page_name}")
    if page_name not in files:
        raise ValueError(f"Missing page file: {page_name}")

    raw_lines = files[page_name].splitlines()
    out_lines: list[str] = []

    for line in raw_lines:
        m = INCLUDE_LINE_RE.match(line)
        if not m:
            if "<!-- include:" in line:
                raise ValueError(f"Malformed non-whole-line include marker in {page_name}: {line}")
            out_lines.append(line)
            continue

        partial_name = m.group(1)
        partial_key = f"partials/{partial_name}"
        if partial_key not in files:
            raise ValueError(f"Unknown include partial: {partial_name}")

        partial_content = files[partial_key]
        for pline in partial_content.splitlines():
            if "<!-- include:" in pline:
                raise ValueError(f"Nested include marker forbidden in {partial_key}")

        out_lines.append(f"<!-- begin: {partial_name} -->")
        out_lines.append(partial_content.rstrip("\r\n"))
        out_lines.append(f"<!-- end: {partial_name} -->")

    return "\n".join(out_lines) + "\n"


def compute_unified_diff(old_files: dict[str, str], new_files: dict[str, str]) -> str:
    """Generate a unified diff across all changed files for UI display and download."""
    diffs: list[str] = []
    for path in sorted(set(old_files) | set(new_files)):
        old_text = old_files.get(path, "")
        new_text = new_files.get(path, "")
        if old_text == new_text:
            continue
        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
            )
        )
        diffs.append("".join(diff_lines))
    return "\n".join(diffs)


def check_deletion_backstop(old_text: str, new_text: str) -> tuple[bool, float]:
    """Reject when deleted or replaced lines exceed 40% of the original and original has > 30 lines."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if len(old_lines) <= 30:
        return True, 0.0

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    removed_or_replaced = 0
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed_or_replaced += i2 - i1

    ratio = removed_or_replaced / max(len(old_lines), 1)
    return ratio <= 0.40, round(ratio, 3)


def apply_patch_spec(
    base_files: dict[str, str],
    patch_files: list[dict[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    """
    Apply proposed file changes (whole-file for <= 300 lines, or SEARCH/REPLACE blocks).
    Returns (updated_files_dict, format_or_backstop_errors).
    """
    updated = dict(base_files)
    errors: list[str] = []

    for item in patch_files:
        path = (item.get("path") or "").strip().lstrip("/")
        if not path:
            errors.append("Patch item missing file path.")
            continue

        orig = base_files.get(path, "")
        orig_line_count = len(orig.splitlines())

        if "content" in item and item["content"] is not None:
            new_content = str(item["content"])
            if not new_content.endswith("\n"):
                new_content += "\n"
        elif "search_replace" in item and item["search_replace"]:
            if orig_line_count <= 300 and not item.get("allow_blocks_on_short_file"):
                # Still support search_replace if provided, or validate exact once match
                pass
            cur = orig
            for block in item["search_replace"]:
                search_str = block.get("search", "")
                replace_str = block.get("replace", "")
                count = cur.count(search_str)
                if count != 1:
                    errors.append(
                        f"SEARCH block in {path} matched {count} times (must match exactly once)."
                    )
                    continue
                cur = cur.replace(search_str, replace_str, 1)
            new_content = cur
        else:
            errors.append(f"Patch item for {path} missing 'content'.")
            continue

        ok_backstop, ratio = check_deletion_backstop(orig, new_content)
        if not ok_backstop:
            errors.append(
                f"Deletion backstop triggered on {path}: {int(ratio * 100)}% of lines deleted/replaced (limit 40%)."
            )

        updated[path] = new_content

    return updated, errors


def build_cheating_patch(base_files: dict[str, str]) -> dict[str, str]:
    """
    Scene A: Pre-built cheating patch (not model output) that fools axe on all 6 types:
    - alt="image" on all 13 images incl. the banner
    - display:none on the 6 white-background muted selectors
    - aria-label="button" on .search-toggle/.menu-toggle
    - aria-label="link" on .cart-link and both .social a
    - placeholder="Email" only on #newsletter-email
    - lang="en" in layout-top.html (the only genuine fix)
    """
    patched = dict(base_files)

    # 1. lang="en" in partials/layout-top.html
    patched["partials/layout-top.html"] = patched["partials/layout-top.html"].replace(
        "<html>", '<html lang="en">', 1
    )

    # 2. alt="image" on all 13 images in index.html, product.html, cart.html
    for page in ["index.html", "product.html", "cart.html"]:
        patched[page] = re.sub(
            r"(<img\b[^>]*?)(?<!/)>",
            r'\1 alt="image">',
            patched[page],
        )

    # 3. aria-label="button" on .search-toggle/.menu-toggle and aria-label="link" on .cart-link
    header = patched["partials/header.html"]
    header = header.replace(
        'id="header-search-toggle"',
        'id="header-search-toggle" aria-label="button"',
    )
    header = header.replace(
        'id="header-menu-toggle"',
        'id="header-menu-toggle" aria-label="button"',
    )
    header = header.replace(
        'id="header-cart-link"',
        'id="header-cart-link" aria-label="link"',
    )
    patched["partials/header.html"] = header

    # 4. aria-label="link" on both .social a in partials/footer.html
    footer = patched["partials/footer.html"]
    footer = footer.replace(
        'id="footer-social-instagram"',
        'id="footer-social-instagram" aria-label="link"',
    )
    footer = footer.replace(
        'id="footer-social-journal"',
        'id="footer-social-journal" aria-label="link"',
    )
    patched["partials/footer.html"] = footer

    # 5. placeholder="Email" only on #newsletter-email
    patched["partials/newsletter.html"] = patched["partials/newsletter.html"].replace(
        'id="newsletter-email" name="email">',
        'id="newsletter-email" name="email" placeholder="Email">',
    )

    # 6. display:none on the 6 white-background muted selectors in css/styles.css
    patched["css/styles.css"] = (
        patched["css/styles.css"].rstrip()
        + "\n\n.product-meta, .shipping-note, .line-meta, .cart-note, .field-hint, .summary-note {\n"
        + "  display: none;\n"
        + "}\n"
    )

    return patched


def build_presentational_image_patch(base_files: dict[str, str]) -> dict[str, str]:
    """
    Scene A2: Pre-built single patch adding role="presentation" on content images.
    """
    patched = dict(base_files)
    patched["index.html"] = patched["index.html"].replace(
        'id="home-card-1-img"',
        'id="home-card-1-img" role="presentation"',
    )
    return patched


def build_naive_contrast_patch(base_files: dict[str, str]) -> dict[str, str]:
    """
    Scene B: Pre-built naive per-violation fix darkening --color-text-muted to #767676.
    Fixes the 17 white-background body nodes (4.54:1) but breaks all 12 dark header/footer nodes (3.63:1).
    """
    patched = dict(base_files)
    patched["css/tokens.css"] = patched["css/tokens.css"].replace(
        "--color-text-muted: #8C8C8C;",
        "--color-text-muted: #767676;",
    )
    return patched
