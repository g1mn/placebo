"""Ground each verdict in the normative text: when Placebo rejects a patch or asks a person to review something, it
cites the WCAG 2.2 success criterion involved, and Tavily fetches the matching W3C "Understanding" page at runtime so
the person at the merge gate reads the source, not our paraphrase.

The success criterion comes from axe-core's own tags (wcag143 -> 1.4.3) or, for Guard and review findings, from a
small fixed table below. Without TAVILY_API_KEY the citation still links the W3C page, marked as not fetched.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

TAVILY_URL = "https://api.tavily.com/search"

# the success criteria this store's checks can touch; slug = W3C Understanding page name
SC = {
    "1.1.1": ("Non-text Content", "non-text-content"),
    "1.3.1": ("Info and Relationships", "info-and-relationships"),
    "1.4.3": ("Contrast (Minimum)", "contrast-minimum"),
    "2.4.4": ("Link Purpose (In Context)", "link-purpose-in-context"),
    "2.4.6": ("Headings and Labels", "headings-and-labels"),
    "2.5.3": ("Label in Name", "label-in-name"),
    "3.1.1": ("Language of Page", "language-of-page"),
    "3.3.2": ("Labels or Instructions", "labels-or-instructions"),
    "4.1.2": ("Name, Role, Value", "name-role-value"),
}

# Guard reasons and review items carry no axe tags: map their wording to the criterion they protect
TEXT_RULES = [
    (r'alt="|content image|role="presentation"|alt text', ["1.1.1"]),
    (r'aria-label="|accessible names?|labels? updated|wording', ["4.1.2", "2.5.3"]),
    (r"contrast|color|stylesheet tokens", ["1.4.3"]),
    (r"<label|placeholder", ["3.3.2", "1.3.1"]),
    (r"lang", ["3.1.1"]),
]

_cache: dict[str, dict[str, Any]] = {}


def sc_from_tag(tag: str) -> str | None:
    d = tag[4:] if tag.startswith("wcag") else ""
    return f"{d[0]}.{d[1]}.{d[2:]}" if len(d) >= 3 and d.isdigit() else None


def criteria_for(keys: list[dict[str, Any]], texts: list[str]) -> list[str]:
    found: list[str] = []
    for k in keys:
        for t in k.get("wcag") or []:
            sc = sc_from_tag(t)
            if sc and sc not in found:
                found.append(sc)
    for text in texts:
        for pattern, scs in TEXT_RULES:
            if re.search(pattern, text, re.I):
                found += [s for s in scs if s not in found]
    return [s for s in found if s in SC]


def _static(sc: str) -> dict[str, Any]:
    name, slug = SC[sc]
    return {"sc": sc, "name": name, "title": f"Understanding SC {sc}: {name}", "source": "static link (not fetched)",
            "url": f"https://www.w3.org/WAI/WCAG22/Understanding/{slug}.html", "quote": ""}


def fetch(sc: str) -> dict[str, Any]:
    """One Tavily search per criterion per process (cached); falls back to the static W3C link."""
    if sc in _cache:
        return _cache[sc]
    cite = _static(sc)
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        body = {"query": f"Understanding Success Criterion {sc} {SC[sc][0]} WCAG 2.2", "search_depth": "basic",
                "include_domains": ["w3.org"], "max_results": 8}
        req = urllib.request.Request(TAVILY_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                results = json.loads(r.read()).get("results") or []
            slug = SC[sc][1]
            # only the current Understanding page for this criterion counts (2.2 before 2.1); old drafts do not
            pages = [x for x in results if re.search(rf"/WAI/WCAG2[12]/Understanding/{slug}\.html", x.get("url", ""))]
            best = min(pages, key=lambda x: "WCAG22" not in x["url"], default=None)
            if best:
                cite = {"sc": sc, "name": SC[sc][0], "title": best.get("title", ""), "url": best.get("url", ""),
                        "quote": re.sub(r"\s+", " ", best.get("content", "")).strip()[:320],
                        "source": "Tavily search (live)", "fetch_ms": int((time.time() - t0) * 1000)}
            if not best:
                cite = {**cite, "source": "static link (Tavily found no Understanding page)"}
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            cite = {**cite, "source": f"static link (Tavily failed: {str(e)[:80]})"}
    _cache[sc] = cite
    return cite


def cite_version(v_obj: dict[str, Any], group: dict[str, Any] | None,
                 baseline_keys: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Citations for one candidate version: the criteria its group targets, failing axe keys, Guard reasons,
    hidden-check and review findings."""
    ev = v_obj.get("eval") or {}
    conds = ev.get("conditions") or {}
    scan_keys = (v_obj.get("scan") or {}).get("keys") or []
    failing = {(k["page"], k["rule"], k["selector"]) for name in ("1_targeted_keys_gone", "2_zero_new_violations")
               for k in (conds.get(name) or {}).get("remaining_targeted" if name.startswith("1") else "new_violations", [])}
    keys = [k for k in scan_keys if (k.get("page"), k.get("rule"), k.get("selector")) in failing]
    rules = {t.get("rule") for t in v_obj.get("targeted_keys") or [] if isinstance(t, dict)}
    keys = [k for k in baseline_keys or [] if k.get("rule") in rules] + keys  # what this patch set out to fix
    guard = v_obj.get("guard") or ev.get("guard") or {}  # serialized/replayed versions keep it under eval
    texts = list(guard.get("reasons") or []) + list(guard.get("human_review_items") or []) \
        + list((group or {}).get("human_review_items") or [])
    return [fetch(sc) for sc in criteria_for(keys, texts)]


if __name__ == "__main__":  # offline self-check (no key needed)
    assert sc_from_tag("wcag143") == "1.4.3" and sc_from_tag("wcag1411") == "1.4.11" and sc_from_tag("wcag2aa") is None
    assert criteria_for([{"wcag": ["wcag143"]}], []) == ["1.4.3"]
    assert criteria_for([], ['Rejected — alt="image" is not a description']) == ["1.1.1"]
    assert "3.1.1" in criteria_for([{"wcag": ["wcag311"]}], [])
    os.environ.pop("TAVILY_API_KEY", None)
    c = fetch("1.4.3")
    assert c["url"].endswith("/contrast-minimum.html") and c["source"].startswith("static")
    print("ok")
