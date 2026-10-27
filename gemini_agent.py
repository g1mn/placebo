from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from dotenv import load_dotenv

# Load local hackathon env if present (never committed)
_local_env = Path.home() / "hack" / ".env.hackathon"
if _local_env.exists():
    load_dotenv(_local_env)
load_dotenv()

ARGON_SEMAPHORE = asyncio.Semaphore(2)
SPEND_CAP_USD = 20.0

# Model pricing estimates (USD per 1M tokens) for spend meter
PRICING_PER_1M = {
    "gemini-4-argon": {"input": 2.50, "cached": 0.25, "output": 12.00},
    "gemini-3.8-flash": {"input": 0.30, "cached": 0.03, "output": 1.20},
    "gemini-3.6-flash": {"input": 0.20, "cached": 0.02, "output": 0.80},
    "gemini-3.5-flash-lite": {"input": 0.10, "cached": 0.01, "output": 0.40},
    "gemini-3.8-flash-tts": {"input": 0.40, "cached": 0.04, "output": 2.00},
}


def get_role_model(role: str) -> str:
    defaults = {
        "FIX": "gemini-4-argon",
        "REVIEW": "gemini-4-argon",
        "ASSIST": "gemini-3.8-flash",
        "ROUTER": "gemini-3.5-flash-lite",
        "TTS": "gemini-3.8-flash-tts",
    }
    return os.environ.get(f"MODEL_{role}", defaults[role])


def get_role_thinking(role: str, force_low: bool = False) -> str:
    if force_low:
        return "low"
    defaults = {
        "FIX": "medium",
        "REVIEW": "low",
        "ASSIST": "low",
        "ROUTER": "low",
    }
    val = os.environ.get(f"THINKING_{role}", defaults.get(role, "low")).lower().strip()
    # high is banned on the live path
    if val == "high":
        return "medium"
    if val not in ("low", "medium"):
        return "low"
    return val


def is_demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "0").strip() == "1"


# Fixed alphabetical order of tools per AGENTS.md §4:
# escalate_group, propose_patch, read_file, run_scan, submit_diagnosis, update
LIVE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "escalate_group",
        "description": "Escalate the current root-cause group to a human reviewer when it cannot be safely fixed during retries.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Clear explanation of why the group requires human intervention.",
                },
                "cannot_fix": {
                    "type": "boolean",
                    "description": "True when the issue cannot be fixed automatically.",
                },
            },
            "required": ["reason", "cannot_fix"],
        },
    },
    {
        "type": "function",
        "name": "propose_patch",
        "description": "Propose complete replacement files (for files <= 300 lines) to fix the targeted root-cause accessibility violation group without introducing regressions.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": "List of store files to update with their full file content.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative path from store root, e.g. css/tokens.css or partials/header.html.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete updated file content (for files <= 300 lines).",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
                "targeted_keys": {
                    "type": "array",
                    "description": "List of {rule, selector} targets addressed by this patch.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule": {"type": "string"},
                            "selector": {"type": "string"},
                        },
                        "required": ["rule", "selector"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "Concise explanation of the root-cause fix and how it preserves dark/light context.",
                },
            },
            "required": ["files", "targeted_keys", "summary"],
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a store source file. Rarely needed because all store source files are already provided in the prompt.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from store root."}
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "run_scan",
        "description": "Read the current cached axe violation list. Rarely needed because baseline violations are already in the prompt and browser re-verification runs automatically after propose_patch.",
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Optional page name."}
            },
        },
    },
    {
        "type": "function",
        "name": "submit_diagnosis",
        "description": "Record the structured root-cause diagnosis for the active violation group.",
        "parameters": {
            "type": "object",
            "properties": {
                "group_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "description": "'patch' to fix automatically, or 'guide' to hand off to human.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Shared root cause across pages and templates.",
                },
                "shared_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Store files responsible for this root cause.",
                },
                "cannot_fix": {"type": "boolean"},
                "escalation_reason": {"type": "string"},
            },
            "required": ["group_id", "mode", "root_cause", "shared_files", "cannot_fix"],
        },
    },
    {
        "type": "function",
        "name": "update",
        "description": "Send a short progress status line to the live UI.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Short status message."}
            },
            "required": ["message"],
        },
    },
]

# Fixed system instruction (never include timestamps or random IDs to preserve implicit caching)
FIXED_SYSTEM_INSTRUCTION = """You are Placebo's accessibility root-cause patching engine for a static HTML/CSS storefront ('Bukchon Wool').
Your job is to fix the active root-cause accessibility violation group at the shared source without fooling the checker or breaking other contexts.

CRITICAL RULES:
1. In your FIRST turn, call `update`, `submit_diagnosis`, and `propose_patch` TOGETHER in a single turn (1 hop). Do not call `read_file` or `run_scan` unless something is missing, because all store source files and baseline violations are already in the prompt.
2. Never use generic placeholder names ('image', 'photo', 'picture', 'icon', 'button', 'link', 'img', or filenames). Use meaningful, accurate accessible names matching the actual UI control or content:
   - Header search toggle button (`#header-search-toggle` / `.search-toggle`): `aria-label="Search"`
   - Header shopping bag link (`#header-cart-link` / `.cart-link`): `aria-label="Cart"`
   - Header menu toggle button (`#header-menu-toggle` / `.menu-toggle`): `aria-label="Menu"`
   - Footer social links (`#footer-social-instagram`, `#footer-social-journal`): `aria-label="Instagram"` and `aria-label="Journal"`
   - Newsletter email input (`#newsletter-email`): add an explicit `<label for="newsletter-email">Email address</label>` (never rely on placeholder or title alone)
   - HTML language (`partials/layout-top.html`): set `<html lang="en">`. This partial is only the top of every page and deliberately leaves `<head>` open (each page adds its own `<title>`, `</head>` and `<body>`), so never add `</head>` or `<body>` to it
   - Color contrast (`css/tokens.css` and `css/styles.css`): notice the dual-context token trap! `--color-text-muted: #8C8C8C` is used both on white `#FFFFFF` in the body (`.product-meta`, `.shipping-note`, `.line-meta`, `.cart-note`, `.field-hint`, `.summary-note` — where `#8C8C8C` fails 4.5:1 at 3.36:1) AND on dark `#1C1F26` in the header/footer (`.utility-note`, `.footer-note` — where `#8C8C8C` passes at 4.90:1). Simply darkening `--color-text-muted` to `#595959` or `#767676` will break contrast on the dark `#1C1F26` header/footer! Instead, split the token by surface role: e.g. set `--color-text-muted: #595959` (7.0:1 on white) for the body classes and introduce `--color-text-muted-on-dark: #A6ADBB` (or keep `#8C8C8C` / `var(--color-text-on-dark-soft)`) for `.utility-note, .footer-note` in `css/styles.css`!
3. Fix ONLY the active group's rule (`active_violations`) in this turn so each root-cause group is verified one at a time. When a file appears under `CURRENT APPROVED FILE OVERRIDES`, you MUST start from that current approved file content and preserve all existing fixes (`aria-label`, `<label>`, `lang="en"`, CSS tokens) already present in it!
4. Never remove or rename any existing `id` attribute. Never delete content, hide elements (`display:none`, `visibility:hidden`, `opacity:0`, `hidden`), or add `<script>` tags.
5. Provide complete whole-file contents in `propose_patch` (all store files are <= 300 lines)."""


FIXED_RULES_BLOCK = """=== PATCH & VERIFICATION RULES ===
- Allowed files (whitelist): index.html, product.html, cart.html, checkout.html, partials/layout-top.html, partials/header.html, partials/newsletter.html, partials/footer.html, css/tokens.css, css/styles.css.
- Every existing element `id` must remain unchanged.
- Every file is <= 300 lines; always supply the full file content in `propose_patch.files`.
- Target keys are given as `{rule, selector}` and are verified across all 4 pages (`index.html`, `product.html`, `cart.html`, `checkout.html`)."""


def build_cached_prefix_and_prompt(
    baseline_files: dict[str, str],
    baseline_keys: list[dict[str, Any]],
    current_files: dict[str, str],
    group_info: dict[str, Any],
) -> str:
    """
    Fixed order for implicit caching:
    system_instruction -> tools -> rules -> source files (fixed sort) -> baseline violations (fixed sort) -> variable part last.
    No timestamps or random IDs in the prefix.
    """
    chunks: list[str] = [FIXED_RULES_BLOCK, "\n=== BASELINE SOURCE FILES (FIXED SORT) ==="]
    for path in sorted(baseline_files.keys()):
        chunks.append(f"--- FILE: {path} ---\n{baseline_files[path]}")

    sorted_base_keys = sorted(
        baseline_keys,
        key=lambda k: (k.get("page", ""), k.get("rule", ""), k.get("selector", "")),
    )
    chunks.append("\n=== BASELINE AXE-CORE VIOLATIONS (58 NODES, FIXED SORT) ===")
    chunks.append(json.dumps(sorted_base_keys, indent=2, ensure_ascii=False))

    # Variable part last: any already-approved modifications + current group target
    modified = {
        p: current_files[p]
        for p in sorted(current_files.keys())
        if current_files.get(p) != baseline_files.get(p)
    }
    if modified:
        chunks.append(
            "\n=== CURRENT APPROVED FILE OVERRIDES (MUST PRESERVE ALL FIXES IN THESE FILES) ==="
        )
        for path, content in modified.items():
            chunks.append(f"--- CURRENT FILE: {path} ---\n{content}")

    chunks.append("\n=== ACTIVE ROOT-CAUSE GROUP TO FIX NOW ===")
    chunks.append(json.dumps(group_info, indent=2, ensure_ascii=False))
    chunks.append(
        "\nCall `update`, `submit_diagnosis`, and `propose_patch` now in this single turn. Fix only the active group's rule while preserving any CURRENT APPROVED FILE OVERRIDES."
    )
    return "\n\n".join(chunks)


class GeminiRuntime:
    def __init__(self) -> None:
        self.verified_models: list[str] = []
        self.models_checked: bool = False
        self.total_spend_usd: float = 0.0
        self.calls_log: list[dict[str, Any]] = []
        self.pin_thinking_low: bool = False
        self.cost_mode: str = os.environ.get("COST_MODE", "save").strip().lower()

    def _get_client(self, timeout_ms: int = 120000):
        from google import genai  # lazy: the default Nebius backend does not need google-genai installed

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured in environment.")
        return genai.Client(api_key=api_key, http_options={"timeout": timeout_ms})

    async def ensure_models_verified(self) -> list[str]:
        if self.models_checked and self.verified_models:
            return self.verified_models

        def _list() -> list[str]:
            client = self._get_client(timeout_ms=20000)
            return [m.name for m in client.models.list()]

        models = await asyncio.to_thread(_list)
        self.verified_models = models
        self.models_checked = True
        return models

    def record_usage(self, model: str, usage_obj: Any) -> dict[str, Any]:
        in_tok = int(getattr(usage_obj, "total_input_tokens", 0) or 0)
        out_tok = int(getattr(usage_obj, "total_output_tokens", 0) or 0)
        cached_tok = int(getattr(usage_obj, "total_cached_tokens", 0) or 0)
        thought_tok = int(getattr(usage_obj, "total_thought_tokens", 0) or 0)
        total_tok = int(getattr(usage_obj, "total_tokens", 0) or (in_tok + out_tok + thought_tok))

        rates = PRICING_PER_1M.get(
            model, {"input": 1.0, "cached": 0.1, "output": 4.0}
        )
        uncached_in = max(0, in_tok - cached_tok)
        cost = (
            (uncached_in / 1_000_000.0) * rates["input"]
            + (cached_tok / 1_000_000.0) * rates["cached"]
            + ((out_tok + thought_tok) / 1_000_000.0) * rates["output"]
        )
        self.total_spend_usd = round(self.total_spend_usd + cost, 6)
        cache_ratio = round((cached_tok / in_tok) * 100.0, 1) if in_tok > 0 else 0.0

        info = {
            "model": model,
            "input_tokens": in_tok,
            "cached_tokens": cached_tok,
            "output_tokens": out_tok,
            "thought_tokens": thought_tok,
            "total_tokens": total_tok,
            "cache_hit_pct": cache_ratio if "argon" in model else None,
            "est_cost_usd": round(cost, 5),
            "cumulative_spend_usd": round(self.total_spend_usd, 4),
        }
        self.calls_log.append(info)
        return info

    async def run_argon_turn(
        self,
        *,
        input_payload: Any,
        previous_interaction_id: str | None,
        is_retry: bool,
        current_files: dict[str, str],
        baseline_scan_summary: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        attempt: int = 0,
    ) -> dict[str, Any]:
        """
        Execute one Argon diagnose+patch round (up to 3 hops if read_file/run_scan/update precedes propose_patch).
        Streams events via on_event callback.
        """
        if self.total_spend_usd >= SPEND_CAP_USD:
            return {
                "status": "handoff",
                "reason": "spend_cap_reached",
                "message": "Escalated to human: cumulative $20 spend cap reached",
            }

        await self.ensure_models_verified()

        demo = is_demo_mode()
        cutoff_s = (40.0 if is_retry else 85.0) if demo else (60.0 if is_retry else 120.0)

        primary_model = get_role_model("FIX")
        fallback_chain = (
            [primary_model]
            if demo
            else [primary_model, "gemini-3.8-flash", "gemini-3.6-flash"]
        )

        last_err = None
        for idx, model_id in enumerate(fallback_chain):
            is_fallback = idx > 0
            # Never switch models mid-history: if falling back, only possible on fresh interaction
            if is_fallback and previous_interaction_id is not None:
                break

            thinking_lvl = get_role_thinking(
                "FIX", force_low=(self.pin_thinking_low or is_fallback)
            )

            try:
                async with ARGON_SEMAPHORE:
                    result = await self._execute_interaction_hops(
                        model_id=model_id,
                        thinking_level=thinking_lvl,
                        input_payload=input_payload,
                        previous_interaction_id=previous_interaction_id,
                        cutoff_s=cutoff_s,
                        is_fallback=is_fallback,
                        current_files=current_files,
                        baseline_scan_summary=baseline_scan_summary,
                        on_event=on_event,
                    )
                if result.get("elapsed_s", 0) > 75.0 and model_id == "gemini-4-argon":
                    self.pin_thinking_low = True
                return result
            except TimeoutError:
                last_err = "timeout"
                if demo:
                    return {
                        "status": "cutoff_replay",
                        "reason": "cutoff_exceeded",
                        "model": model_id,
                    }
                continue
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    self.cost_mode = "save"
                last_err = err_str
                if demo:
                    break
                continue

        return {
            "status": "error",
            "reason": last_err or "model_call_failed",
            "model": primary_model,
        }

    async def _execute_interaction_hops(
        self,
        *,
        model_id: str,
        thinking_level: str,
        input_payload: Any,
        previous_interaction_id: str | None,
        cutoff_s: float,
        is_fallback: bool,
        current_files: dict[str, str],
        baseline_scan_summary: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        t_start = time.time()
        prev_id = previous_interaction_id
        cur_input = input_payload
        diagnosis: dict[str, Any] | None = None
        proposed_patch: dict[str, Any] | None = None
        propose_call_id: str | None = None
        pending_companion_results: list[dict[str, Any]] = []
        last_usage_info: dict[str, Any] = {}

        # Up to 3 hops per round
        for hop in range(1, 4):
            remaining_s = max(5.0, cutoff_s - (time.time() - t_start))
            gen_cfg: dict[str, Any] = {
                "thinking_level": thinking_level,
                "max_output_tokens": 32768,
            }
            if "argon" in model_id:
                gen_cfg["thinking_summaries"] = "auto"

            if on_event:
                on_event(
                    {
                        "type": "model_start",
                        "model": model_id,
                        "is_fallback": is_fallback,
                        "thinking_level": thinking_level,
                        "hop": hop,
                        "elapsed_s": round(time.time() - t_start, 1),
                    }
                )

            def _call_stream() -> dict[str, Any]:
                client = self._get_client(timeout_ms=int(remaining_s * 1000))
                kwargs: dict[str, Any] = {
                    "model": model_id,
                    "input": cur_input,
                    "system_instruction": FIXED_SYSTEM_INSTRUCTION,
                    "tools": LIVE_TOOLS,
                    "generation_config": gen_cfg,
                    "stream": True,
                    "timeout": remaining_s,
                }
                if prev_id:
                    kwargs["previous_interaction_id"] = prev_id
                stream = client.interactions.create(**kwargs)
                final_interaction = None
                steps_by_idx: dict[int, dict[str, Any]] = {}
                for ev in stream:
                    if time.time() - t_start > cutoff_s:
                        raise TimeoutError("Round cutoff exceeded")
                    ev_dict = ev.to_dict() if hasattr(ev, "to_dict") else {}
                    ev_type = ev_dict.get("event_type") or getattr(ev, "event_type", "") or ""
                    idx = ev_dict.get("index", getattr(ev, "index", 0))
                    if ev_type == "step.start":
                        step_d = ev_dict.get("step") or {}
                        s_type = step_d.get("type", "")
                        steps_by_idx[idx] = {
                            "type": s_type,
                            "id": step_d.get("id", ""),
                            "name": step_d.get("name", ""),
                            "args_buf": "",
                            "initial_args": step_d.get("arguments") or {},
                            "text_buf": "",
                        }
                        if s_type == "thought" and on_event:
                            on_event(
                                {
                                    "type": "thinking",
                                    "model": model_id,
                                    "text": f"thinking… ({round(time.time() - t_start, 1)} s)",
                                    "elapsed_s": round(time.time() - t_start, 1),
                                }
                            )
                        elif s_type == "function_call" and on_event:
                            on_event(
                                {
                                    "type": "tool_call_start",
                                    "model": model_id,
                                    "name": step_d.get("name", ""),
                                    "elapsed_s": round(time.time() - t_start, 1),
                                }
                            )
                    elif ev_type == "step.delta":
                        delta_d = ev_dict.get("delta") or {}
                        entry = steps_by_idx.get(idx)
                        if entry is not None:
                            if "arguments" in delta_d and delta_d["arguments"]:
                                entry["args_buf"] += str(delta_d["arguments"])
                            txt = delta_d.get("text") or delta_d.get("thought")
                            if txt:
                                entry["text_buf"] += str(txt)
                                if on_event:
                                    on_event(
                                        {
                                            "type": "thinking_summary",
                                            "model": model_id,
                                            "text": str(txt)[:240],
                                            "elapsed_s": round(time.time() - t_start, 1),
                                        }
                                    )
                    elif ev_type == "interaction.completed":
                        final_interaction = getattr(ev, "interaction", None)

                parsed_calls: list[dict[str, Any]] = []
                for k in sorted(steps_by_idx.keys()):
                    st = steps_by_idx[k]
                    if st["type"] == "function_call":
                        raw_args = st["args_buf"].strip()
                        args_obj = st["initial_args"]
                        if raw_args:
                            try:
                                args_obj = json.loads(raw_args)
                            except Exception:
                                args_obj = {}
                        parsed_calls.append(
                            {
                                "id": st["id"],
                                "name": st["name"],
                                "arguments": args_obj if isinstance(args_obj, dict) else {},
                            }
                        )
                return {
                    "interaction": final_interaction,
                    "function_calls": parsed_calls,
                }

            stream_res = await asyncio.wait_for(
                asyncio.to_thread(_call_stream), timeout=remaining_s + 2.0
            )
            interaction = stream_res["interaction"]
            function_calls = stream_res["function_calls"]

            # Retry once at low if status is incomplete in DEMO_MODE=0
            if (
                interaction is not None
                and getattr(interaction, "status", "") == "incomplete"
                and not is_demo_mode()
                and thinking_level != "low"
            ):
                thinking_level = "low"
                continue

            if interaction is None:
                raise RuntimeError("No completed interaction returned from stream.")

            prev_id = interaction.id
            last_usage_info = self.record_usage(model_id, getattr(interaction, "usage", None))

            if not function_calls:
                break

            next_results: list[dict[str, Any]] = []
            for fc in function_calls:
                fname = fc["name"]
                fargs = fc["arguments"] or {}
                cid = fc["id"]

                if fname == "update":
                    msg = fargs.get("message", "Working…")
                    if on_event:
                        on_event(
                            {
                                "type": "agent_update",
                                "model": model_id,
                                "message": msg,
                                "elapsed_s": round(time.time() - t_start, 1),
                            }
                        )
                    res_item = {
                        "type": "function_result",
                        "name": "update",
                        "call_id": cid,
                        "result": json.dumps({"status": "ok"}),
                    }
                    next_results.append(res_item)
                    pending_companion_results.append(res_item)

                elif fname == "submit_diagnosis":
                    diagnosis = {
                        "group_id": fargs.get("group_id", ""),
                        "mode": fargs.get("mode", "patch"),
                        "root_cause": fargs.get("root_cause", ""),
                        "shared_files": fargs.get("shared_files", []),
                        "cannot_fix": bool(fargs.get("cannot_fix", False)),
                        "escalation_reason": fargs.get("escalation_reason", ""),
                    }
                    if on_event:
                        on_event(
                            {
                                "type": "diagnosis",
                                "model": model_id,
                                "diagnosis": diagnosis,
                                "elapsed_s": round(time.time() - t_start, 1),
                            }
                        )
                    res_item = {
                        "type": "function_result",
                        "name": "submit_diagnosis",
                        "call_id": cid,
                        "result": json.dumps({"status": "recorded"}),
                    }
                    next_results.append(res_item)
                    pending_companion_results.append(res_item)

                elif fname == "escalate_group":
                    return {
                        "status": "escalated",
                        "model": model_id,
                        "is_fallback": is_fallback,
                        "interaction_id": prev_id,
                        "diagnosis": diagnosis,
                        "reason": fargs.get("reason", "Model escalated group to human"),
                        "cannot_fix": bool(fargs.get("cannot_fix", True)),
                        "usage": last_usage_info,
                        "elapsed_s": round(time.time() - t_start, 2),
                    }

                elif fname == "propose_patch":
                    proposed_patch = {
                        "files": fargs.get("files", []),
                        "targeted_keys": fargs.get("targeted_keys", []),
                        "summary": fargs.get("summary", ""),
                    }
                    propose_call_id = cid
                    if on_event:
                        on_event(
                            {
                                "type": "patch_proposed",
                                "model": model_id,
                                "summary": proposed_patch["summary"],
                                "files": [f.get("path") for f in proposed_patch["files"]],
                                "elapsed_s": round(time.time() - t_start, 1),
                            }
                        )

                elif fname == "read_file":
                    req_path = (fargs.get("path") or "").strip().lstrip("/")
                    content = current_files.get(req_path, "ERROR: File not found")
                    next_results.append(
                        {
                            "type": "function_result",
                            "name": "read_file",
                            "call_id": cid,
                            "result": content,
                        }
                    )

                elif fname == "run_scan":
                    next_results.append(
                        {
                            "type": "function_result",
                            "name": "run_scan",
                            "call_id": cid,
                            "result": json.dumps(baseline_scan_summary),
                        }
                    )

            # If diagnosis says mode=='guide' or cannot_fix==True without propose_patch, hand off immediately
            if diagnosis and (diagnosis.get("mode") == "guide" or diagnosis.get("cannot_fix")):
                return {
                    "status": "escalated",
                    "model": model_id,
                    "is_fallback": is_fallback,
                    "interaction_id": prev_id,
                    "diagnosis": diagnosis,
                    "reason": diagnosis.get("escalation_reason")
                    or ("mode=guide" if diagnosis.get("mode") == "guide" else "cannot_fix"),
                    "cannot_fix": bool(diagnosis.get("cannot_fix", True)),
                    "usage": last_usage_info,
                    "elapsed_s": round(time.time() - t_start, 2),
                }

            if proposed_patch is not None:
                return {
                    "status": "patch_proposed",
                    "model": model_id,
                    "is_fallback": is_fallback,
                    "thinking_level": thinking_level,
                    "interaction_id": prev_id,
                    "propose_call_id": propose_call_id,
                    "companion_results": pending_companion_results,
                    "diagnosis": diagnosis,
                    "patch": proposed_patch,
                    "usage": last_usage_info,
                    "elapsed_s": round(time.time() - t_start, 2),
                }

            # Otherwise send function_results for this hop and continue loop
            cur_input = next_results
            pending_companion_results = []

        return {
            "status": "no_patch",
            "model": model_id,
            "is_fallback": is_fallback,
            "interaction_id": prev_id,
            "diagnosis": diagnosis,
            "usage": last_usage_info,
            "elapsed_s": round(time.time() - t_start, 2),
        }

    async def draft_alt_candidates_for_image(
        self,
        *,
        image_id: str,
        image_src: str,
        page_context: str,
        image_bytes: bytes | None,
    ) -> dict[str, Any]:
        """
        image-alt never goes through Argon: MODEL_ASSIST (gemini-3.8-flash, fallback on 503)
        drafts 3 candidates per image in parallel. Send image as a separate part.
        """
        models_to_try = [get_role_model("ASSIST"), "gemini-3.6-flash", "gemini-3.5-flash-lite"]
        prompt = (
            f"Draft exactly 3 concise, specific English alt text candidates (each 4 to 12 words) "
            f"for image #{image_id} ({image_src}) on a Seoul knitwear store ('Bukchon Wool'). "
            f"Context around image: {page_context}. "
            f"Never use generic words like 'image', 'photo', 'picture', 'icon', 'banner', or filenames. "
            f"Return ONLY a JSON array of 3 strings, e.g. [\"...\", \"...\", \"...\"]."
        )

        for idx, m_id in enumerate(models_to_try):
            try:
                def _call() -> Any:
                    client = self._get_client(timeout_ms=25000)
                    parts: list[Any] = []
                    if image_bytes:
                        parts.append(
                            {
                                "type": "image",
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        )
                    parts.append({"type": "text", "text": prompt})
                    return client.interactions.create(
                        model=m_id,
                        input=parts,
                        generation_config={
                            "thinking_level": get_role_thinking("ASSIST"),
                            "max_output_tokens": 2048,
                        },
                        timeout=25.0,
                    )

                inter = await asyncio.to_thread(_call)
                usage_info = self.record_usage(m_id, getattr(inter, "usage", None))
                raw_txt = ""
                for s in getattr(inter, "steps", []) or []:
                    if getattr(s, "type", "") == "model_output":
                        for c in getattr(s, "content", []) or []:
                            if getattr(c, "text", None):
                                raw_txt += c.text
                raw_txt = raw_txt.strip()
                # Extract JSON array
                m_arr = re.search(r"\[.*\]", raw_txt, re.DOTALL)
                if m_arr:
                    arr = json.loads(m_arr.group(0))
                    if isinstance(arr, list) and len(arr) >= 3:
                        return {
                            "image_id": image_id,
                            "image_src": image_src,
                            "candidates": [str(x).strip() for x in arr[:3]],
                            "model": m_id,
                            "is_fallback": idx > 0,
                            "usage": usage_info,
                        }
            except Exception:
                continue

        # Deterministic contextual fallback only if all 3 models return 503
        return {
            "image_id": image_id,
            "image_src": image_src,
            "candidates": [
                f"{page_context} product view",
                f"Bukchon Wool {page_context.lower()}",
                f"Studio photograph of {page_context.lower()}",
            ],
            "model": models_to_try[-1],
            "is_fallback": True,
            "usage": {},
        }

    async def run_wording_guard(self, diff_text: str) -> dict[str, Any]:
        """
        Optional flash-lite wording guard (MODEL_ROUTER = gemini-3.5-flash-lite).
        Can only add items to the human review list, never remove.
        """
        m_id = get_role_model("ROUTER")
        try:
            def _call() -> Any:
                client = self._get_client(timeout_ms=15000)
                return client.interactions.create(
                    model=m_id,
                    input=(
                        "Review this accessibility patch diff. List any visible wording or label changes "
                        "that a human editor should verify. Return a JSON array of short strings (max 2 items).\n\n"
                        + diff_text[:3000]
                    ),
                    generation_config={
                        "thinking_level": get_role_thinking("ROUTER"),
                        "max_output_tokens": 1024,
                    },
                    timeout=15.0,
                )

            inter = await asyncio.to_thread(_call)
            usage = self.record_usage(m_id, getattr(inter, "usage", None))
            txt = ""
            for s in getattr(inter, "steps", []) or []:
                if getattr(s, "type", "") == "model_output":
                    for c in getattr(s, "content", []) or []:
                        if getattr(c, "text", None):
                            txt += c.text
            import re as _re
            m_json = _re.search(r"\[.*\]", txt, _re.DOTALL)
            items = json.loads(m_json.group(0)) if m_json else []
            return {
                "model": m_id,
                "items": [str(x) for x in items if str(x).strip()][:2],
                "usage": usage,
            }
        except Exception:
            return {"model": m_id, "items": [], "usage": {}}

    async def synthesize_tts(self, text: str) -> dict[str, Any]:
        """
        Scene C: Simulated screen-reader readout voiced by Gemini TTS (MODEL_TTS = gemini-3.8-flash-tts).
        """
        m_id = get_role_model("TTS")
        t0 = time.time()

        def _call() -> Any:
            client = self._get_client(timeout_ms=20000)
            return client.interactions.create(
                model=m_id,
                input=f"Read aloud clearly like a screen reader: {text}",
                response_modalities=["audio"],
                timeout=20.0,
            )

        inter = await asyncio.to_thread(_call)
        usage = self.record_usage(m_id, getattr(inter, "usage", None))
        audio_b64 = ""
        mime_type = "audio/wav"
        for s in getattr(inter, "steps", []) or []:
            if getattr(s, "type", "") == "model_output":
                for c in getattr(s, "content", []) or []:
                    if getattr(c, "data", None):
                        audio_b64 = c.data
                        mime_type = getattr(c, "mime_type", "audio/wav") or "audio/wav"
                        break

        return {
            "model": m_id,
            "text": text,
            "mime_type": mime_type,
            "audio_data_url": f"data:{mime_type};base64,{audio_b64}" if audio_b64 else "",
            "elapsed_s": round(time.time() - t0, 2),
            "usage": usage,
        }

    async def diagnose_aviato(
        self,
        *,
        axe_summary: dict[str, Any],
        soundcheck_findings: list[dict[str, Any]],
        css_excerpt: str,
    ) -> dict[str, Any]:
        """
        Scene D: Feed Aviato axe scan + three deterministic 'Passes axe — flagged by Placebo'
        findings + shared CSS to Argon diagnosis (~15-25s).
        """
        m_id = get_role_model("FIX")
        t0 = time.time()
        prompt = (
            "You are diagnosing a real-world third-party Bootstrap e-commerce template (Themefisher Aviato, 5 pages).\n"
            "Analyze both the axe-core violations AND the three 'Passes axe — flagged by Placebo' findings below, "
            "plus the shared CSS excerpt.\n"
            "Return a JSON object with keys:\n"
            "- `headline`: 1-sentence executive summary\n"
            "- `shared_root_causes`: array of {title, files, axe_impact, soundcheck_catch, fix_strategy}\n"
            "- `why_axe_alone_is_not_enough`: 2-sentence explanation referencing the 3 Placebo findings.\n\n"
            f"AXE SUMMARY:\n{json.dumps(axe_summary, indent=2)}\n\n"
            f"PLACEBO DETERMINISTIC FINDINGS:\n{json.dumps(soundcheck_findings, indent=2)}\n\n"
            f"SHARED CSS EXCERPT (css/style.css):\n{css_excerpt[:4000]}"
        )

        async with ARGON_SEMAPHORE:
            def _call() -> Any:
                client = self._get_client(timeout_ms=45000)
                return client.interactions.create(
                    model=m_id,
                    input=prompt,
                    generation_config={
                        "thinking_level": "low",
                        "max_output_tokens": 32768,
                    },
                    timeout=45.0,
                )

            inter = await asyncio.to_thread(_call)

        usage = self.record_usage(m_id, getattr(inter, "usage", None))
        txt = ""
        for s in getattr(inter, "steps", []) or []:
            if getattr(s, "type", "") == "model_output":
                for c in getattr(s, "content", []) or []:
                    if getattr(c, "text", None):
                        txt += c.text

        import re as _re
        m_obj = _re.search(r"\{.*\}", txt, _re.DOTALL)
        parsed = {}
        if m_obj:
            try:
                parsed = json.loads(m_obj.group(0))
            except Exception:
                parsed = {"headline": txt[:300], "shared_root_causes": [], "why_axe_alone_is_not_enough": ""}
        else:
            parsed = {"headline": txt[:300], "shared_root_causes": [], "why_axe_alone_is_not_enough": ""}

        return {
            "model": m_id,
            "diagnosis": parsed,
            "elapsed_s": round(time.time() - t0, 2),
            "usage": usage,
        }


import re
