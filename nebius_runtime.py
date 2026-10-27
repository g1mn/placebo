"""Placebo model runtime on Nebius Token Factory + NVIDIA Nemotron (OpenAI-compatible chat completions).

Drop-in replacement for gemini_agent.GeminiRuntime: same public methods and return shapes, so main.py and the
deterministic Guard (guard.py) are unchanged. The Gemini Interactions API kept conversation state server-side
(previous_interaction_id); here we keep the message history client-side and hand out our own interaction ids.

Roles (override with MODEL_<ROLE> env vars):
  FIX / REVIEW  nvidia/nemotron-3-super-120b-a12b   root-cause diagnosis + whole-file patch via tool calls
  ASSIST        nvidia/Nemotron-3.5-Super-VL         sees the product photo, drafts alt-text candidates
  ROUTER        nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B wording guard (can only add items to the human review list)
  ESCALATE      nvidia/Nemotron-3-Ultra-550b-a55b    second retry only: Super → Super (with verifier feedback) → Ultra
Scene C readout is voiced in the browser (Web Speech API), so there is no server TTS model.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

for _p in (Path.home() / ".config" / "placebo" / ".env",):
    if _p.exists():
        load_dotenv(_p)
load_dotenv()

from gemini_agent import FIXED_SYSTEM_INSTRUCTION, LIVE_TOOLS  # prompts and tool schemas are model-agnostic

BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1").rstrip("/")
NEMOTRON_SEMAPHORE = asyncio.Semaphore(2)
log = logging.getLogger("placebo.nebius")
SPEND_CAP_USD = float(os.environ.get("SPEND_CAP_USD", "10"))

# USD per 1M tokens from GET /v1/models?verbose=true (checked 2026-10-27) for the on-screen spend meter;
# Token Factory billing is the source of truth
PRICING_PER_1M = {
    "nvidia/nemotron-3-super-120b-a12b": {"input": 0.30, "cached": 0.06, "output": 0.90},
    "nvidia/Nemotron-3-Ultra-550b-a55b": {"input": 1.00, "cached": 0.12, "output": 3.00},
    "nvidia/Nemotron-3.5-Super-VL": {"input": 0.30, "cached": 0.06, "output": 0.90},
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {"input": 0.06, "output": 0.24},
    "nvidia/Nemotron-3_5-Lightning": {"input": 0.06, "output": 0.24},
}

DEFAULTS = {
    "FIX": "nvidia/nemotron-3-super-120b-a12b",
    "REVIEW": "nvidia/nemotron-3-super-120b-a12b",
    "ASSIST": "nvidia/Nemotron-3.5-Super-VL",
    "ROUTER": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "ESCALATE": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "TTS": "browser-web-speech",
}


def get_role_model(role: str) -> str:
    return os.environ.get(f"MODEL_{role}", DEFAULTS[role])


def get_role_thinking(role: str, force_low: bool = False) -> str:
    return "low" if force_low else os.environ.get(f"THINKING_{role}", "medium" if role == "FIX" else "low")


def is_demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "0").strip() == "1"


OPENAI_TOOLS = [
    {"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}} for t in LIVE_TOOLS
]


def _loads(v: Any) -> Any:
    # Nemotron sometimes double-encodes nested JSON (tool arguments, files, search_replace) as strings, and sometimes
    # the string holds the complete value followed by the remaining arguments (', "targeted_keys": [...]'): keep the
    # first complete JSON value and drop the trailing text; or the last object of an array is missing its "}"
    while isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError as e:
            if e.msg == "Extra data":
                v = json.JSONDecoder().raw_decode(v.lstrip())[0]
            elif v.lstrip()[:1] in ("[", "{") and (fixed := _repair(v)) != v:
                v = fixed  # parse the repaired text on the next pass (it may still carry trailing arguments)
            else:
                break
    return v


def _repair(s: str) -> str:
    """Repairs for the corruptions we captured in propose_patch.files (an array of {path, content} objects only,
    so a string value is never followed by ']' and a backslash before whitespace was meant as a newline)."""
    s = re.sub(r'(?<!\\)"\s*\](?=\s*[,}])', '"', s)                 # "...css\n"], "path": ...  /  "..."]}]
    s = re.sub(r'(?<!\\)\\(?=[ \t])', r'\\n', s)                  # </div>\    <div  ->  </div>\n    <div
    s = re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r'\\\\', s)     # any other invalid escape: keep a literal backslash
    if s.rstrip().endswith('"]'):                                        # last object lost its closing brace
        s = s.rstrip()[:-1] + "}]"
    return s


def _patch_files(raw: Any) -> list[dict[str, Any]]:
    files = _loads(raw)
    if isinstance(raw, str) and os.environ.get("PLACEBO_QUIRK_DUMP"):  # debug: keep raw strings for the feedback report
        with open(os.environ["PLACEBO_QUIRK_DUMP"], "a") as fh:
            fh.write(json.dumps({"raw": raw}) + "\n")
    files = [files] if isinstance(files, dict) else files if isinstance(files, list) else []
    out = []
    for f in map(_loads, files):
        if not isinstance(f, dict) or not str(f.get("path") or "").strip():
            continue
        if f.get("search_replace") is not None:
            sr = _loads(f["search_replace"])
            f["search_replace"] = [b for b in map(_loads, sr if isinstance(sr, list) else [sr]) if isinstance(b, dict)]
        if f.get("content") is not None or f.get("search_replace"):
            out.append(f)
    if isinstance(raw, str) or out != files:
        log.warning("nemotron quirk: propose_patch.files arrived as %s, decoded to %d file(s)", type(raw).__name__, len(out))
    return out


def _chat(body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is not configured in environment.")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    deadline = time.time() + timeout_s
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, deadline - time.time())) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            # overload / rate limit: the request was refused before any work, so a resend cannot double-bill
            if e.code not in (429, 503, 529) or attempt == 3 or deadline - time.time() < 10:
                raise RuntimeError(f"Token Factory {e.code}: {detail}") from e
            try:
                wait = min(10.0, float(e.headers.get("Retry-After") or 3 * attempt))
            except ValueError:
                wait = 3.0 * attempt
            log.warning("Token Factory %s (%s), retrying in %ss", e.code, detail[:80], wait)
            time.sleep(wait)
        except urllib.error.URLError as e:  # connect/TLS failure: the request never reached the model, safe to resend
            if attempt == 3 or deadline - time.time() < 5:
                raise
            log.warning("Token Factory connection failed (%s), retrying", e.reason)
            time.sleep(2)


def _text(resp: dict[str, Any]) -> str:
    return (resp["choices"][0]["message"].get("content") or "").strip()


class NebiusRuntime:
    def __init__(self) -> None:
        self.verified_models: list[str] = []
        self.models_checked = False
        self.total_spend_usd = 0.0
        self.calls_log: list[dict[str, Any]] = []
        self.pin_thinking_low = False
        self.cost_mode = os.environ.get("COST_MODE", "save").strip().lower()
        self._histories: dict[str, list[dict[str, Any]]] = {}

    async def ensure_models_verified(self) -> list[str]:
        if self.models_checked and self.verified_models:
            return self.verified_models

        def _list() -> list[str]:
            key = os.environ.get("NEBIUS_API_KEY", "").strip()
            req = urllib.request.Request(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return [m["id"] for m in json.loads(r.read()).get("data", [])]

        self.verified_models = await asyncio.to_thread(_list)
        self.models_checked = True
        return self.verified_models

    def record_usage(self, model: str, usage: dict[str, Any] | None) -> dict[str, Any]:
        usage = usage or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        thought = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        rates = PRICING_PER_1M.get(model, {"input": 0.3, "output": 0.9})
        # completion_tokens already include reasoning_tokens (prompt + completion == total)
        cost = ((in_tok - cached) * rates["input"] + cached * rates.get("cached", rates["input"])
                + out_tok * rates["output"]) / 1e6
        self.total_spend_usd = round(self.total_spend_usd + cost, 6)
        info = {
            "model": model,
            "provider": "Nebius Token Factory",
            "input_tokens": in_tok,
            "cached_tokens": cached,
            "output_tokens": out_tok - thought,
            "thought_tokens": thought,
            "total_tokens": int(usage.get("total_tokens") or in_tok + out_tok),
            "cache_hit_pct": round(cached / in_tok * 100, 1) if in_tok else None,
            "est_cost_usd": round(cost, 5),
            "cumulative_spend_usd": round(self.total_spend_usd, 4),
        }
        self.calls_log.append(info)
        return info

    # ── diagnose + patch round (tool calls), same contract as GeminiRuntime.run_argon_turn ──
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
        if self.total_spend_usd >= SPEND_CAP_USD:
            return {"status": "handoff", "reason": "spend_cap_reached",
                    "message": f"Escalated to human: cumulative ${SPEND_CAP_USD:g} spend cap reached"}
        demo = is_demo_mode()
        short = is_retry and attempt < 2  # the Ultra escalation is a slower model: give it the first-try budget
        cutoff_s = (40.0 if short else 85.0) if demo else (60.0 if short else 120.0)
        model_id = get_role_model("ESCALATE" if attempt >= 2 else "FIX")
        try:
            async with NEMOTRON_SEMAPHORE:
                return await self._hops(model_id, input_payload, previous_interaction_id, cutoff_s,
                                        current_files, baseline_scan_summary, on_event)
        except TimeoutError:
            return {"status": "cutoff_replay", "reason": "cutoff_exceeded", "model": model_id} if demo else \
                   {"status": "error", "reason": "timeout", "model": model_id}
        except Exception as e:  # network, 4xx/5xx — hand the group to a human like the Gemini path
            return {"status": "error", "reason": str(e)[:300], "model": model_id}

    def _messages_for(self, prev_id: str | None, payload: Any) -> list[dict[str, Any]]:
        msgs = list(self._histories.get(prev_id, [])) if prev_id else [{"role": "system", "content": FIXED_SYSTEM_INSTRUCTION}]
        if isinstance(payload, str):
            msgs.append({"role": "user", "content": payload})
        else:  # list of {"type":"function_result","call_id","result"} — tool results for the last assistant turn
            for item in payload or []:
                msgs.append({"role": "tool", "tool_call_id": item["call_id"], "content": str(item.get("result", ""))})
        return msgs

    async def _hops(self, model_id, input_payload, prev_id, cutoff_s, current_files, scan_summary, on_event):
        t0 = time.time()
        emit = on_event or (lambda e: None)
        msgs = self._messages_for(prev_id, input_payload)
        diagnosis = None
        usage_sum = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "thought_tokens": 0,
                     "total_tokens": 0, "est_cost_usd": 0.0}
        usage_info: dict[str, Any] = {}
        inter_id = prev_id
        for hop in range(1, 5):
            remaining = max(5.0, cutoff_s - (time.time() - t0))
            emit({"type": "model_start", "model": model_id, "is_fallback": False, "thinking_level": "medium",
                  "hop": hop, "elapsed_s": round(time.time() - t0, 1)})
            body = {"model": model_id, "messages": msgs, "tools": OPENAI_TOOLS, "tool_choice": "auto",
                    "max_tokens": 32768, "temperature": 0.2}
            resp = await asyncio.wait_for(asyncio.to_thread(_chat, body, remaining), timeout=remaining + 2)
            u = self.record_usage(model_id, resp.get("usage"))
            for k in usage_sum:  # report the whole attempt, not just its last hop
                usage_sum[k] = round(usage_sum[k] + (u.get(k) or 0), 6)
            usage_info = {**u, **usage_sum, "hops": hop, "cache_hit_pct": round(
                usage_sum["cached_tokens"] / usage_sum["input_tokens"] * 100, 1) if usage_sum["input_tokens"] else None}
            msg = resp["choices"][0]["message"]
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if reasoning:
                emit({"type": "thinking_summary", "model": model_id, "text": str(reasoning)[:240],
                      "elapsed_s": round(time.time() - t0, 1)})
            calls = msg.get("tool_calls") or []
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                        else {"role": "assistant", "content": msg.get("content") or ""})
            inter_id = f"nb-{uuid.uuid4().hex[:12]}"
            self._histories[inter_id] = msgs
            if not calls:
                break
            # every tool_call id gets exactly one tool reply (OpenAI-compatible APIs reject unanswered ids)
            proposals, tool_results = [], []
            for c in calls:
                name = c["function"]["name"]
                args = _loads(c["function"].get("arguments") or "{}")
                if not isinstance(args, dict):
                    log.warning("nemotron quirk: %s arguments not an object: %r", name, str(args)[:200])
                    args = {}
                cid = c["id"]

                def reply(result: Any, name: str = name, cid: str = cid) -> None:
                    tool_results.append({"type": "function_result", "name": name, "call_id": cid,
                                         "result": result if isinstance(result, str) else json.dumps(result)})

                emit({"type": "tool_call_start", "model": model_id, "name": name, "elapsed_s": round(time.time() - t0, 1)})
                if name == "update":
                    emit({"type": "agent_update", "model": model_id, "message": args.get("message", "Working…"),
                          "elapsed_s": round(time.time() - t0, 1)})
                    reply({"status": "ok"})
                elif name == "submit_diagnosis":
                    diagnosis = {"group_id": args.get("group_id", ""), "mode": args.get("mode", "patch"),
                                 "root_cause": args.get("root_cause", ""), "shared_files": args.get("shared_files", []),
                                 "cannot_fix": bool(args.get("cannot_fix", False)),
                                 "escalation_reason": args.get("escalation_reason", "")}
                    emit({"type": "diagnosis", "model": model_id, "diagnosis": diagnosis, "elapsed_s": round(time.time() - t0, 1)})
                    reply({"status": "recorded"})
                elif name == "escalate_group":
                    return {"status": "escalated", "model": model_id, "is_fallback": False, "interaction_id": inter_id,
                            "diagnosis": diagnosis, "reason": args.get("reason", "Model escalated group to human"),
                            "cannot_fix": bool(args.get("cannot_fix", True)), "usage": usage_info,
                            "elapsed_s": round(time.time() - t0, 2)}
                elif name == "propose_patch":
                    files = _patch_files(args.get("files", []))
                    if files:
                        proposals.append((cid, files, args))
                    else:  # malformed files argument: say so in-conversation instead of staging an empty patch
                        log.warning("nemotron quirk: unusable propose_patch.files %r", str(args.get("files"))[:300])
                        reply({"status": "rejected", "error": "files must be a JSON array of objects with path and "
                               "content (or search_replace). Your files argument could not be parsed. Call propose_patch again."})
                elif name == "read_file":
                    reply(current_files.get((args.get("path") or "").strip().lstrip("/"), "ERROR: File not found"))
                elif name == "run_scan":
                    reply(scan_summary)
                else:
                    reply({"status": "error", "error": f"unknown tool {name}"})
            if diagnosis and (diagnosis.get("mode") == "guide" or diagnosis.get("cannot_fix")):
                return {"status": "escalated", "model": model_id, "is_fallback": False, "interaction_id": inter_id,
                        "diagnosis": diagnosis, "reason": diagnosis.get("escalation_reason") or "cannot_fix",
                        "cannot_fix": True, "usage": usage_info, "elapsed_s": round(time.time() - t0, 2)}
            if proposals:
                # one call per file is a valid reading of the tool: merge them (a later call wins for the same path)
                merged: dict[str, dict[str, Any]] = {}
                keys: list[dict[str, Any]] = []
                summaries: list[str] = []
                for _, files, a in proposals:
                    for f in files:
                        merged[str(f["path"]).strip().lstrip("/")] = f
                    tk = _loads(a.get("targeted_keys"))
                    tk = [tk] if isinstance(tk, dict) else tk if isinstance(tk, list) else []
                    keys += [k for k in map(_loads, tk) if isinstance(k, dict) and k not in keys]
                    if a.get("summary"):
                        summaries.append(str(a["summary"]))
                propose_id = proposals[-1][0]
                for pcid, _, _ in proposals[:-1]:
                    tool_results.append({"type": "function_result", "name": "propose_patch", "call_id": pcid,
                                         "result": json.dumps({"status": "merged into your last propose_patch call"})})
                patch = {"files": list(merged.values()), "targeted_keys": keys, "summary": "; ".join(summaries)}
                emit({"type": "patch_proposed", "model": model_id, "summary": patch["summary"],
                      "files": list(merged), "elapsed_s": round(time.time() - t0, 1)})
                # the retry path (main.py) answers propose_call_id with verifier feedback and replays these replies
                return {"status": "patch_proposed", "model": model_id, "is_fallback": False, "thinking_level": "medium",
                        "interaction_id": inter_id, "propose_call_id": propose_id, "companion_results": tool_results,
                        "diagnosis": diagnosis, "patch": patch, "usage": usage_info,
                        "elapsed_s": round(time.time() - t0, 2)}
            for r in tool_results:
                msgs.append({"role": "tool", "tool_call_id": r["call_id"], "content": str(r["result"])})
        return {"status": "no_patch", "model": model_id, "is_fallback": False, "interaction_id": inter_id,
                "diagnosis": diagnosis, "usage": usage_info, "elapsed_s": round(time.time() - t0, 2)}

    async def draft_alt_candidates_for_image(self, *, image_id: str, image_src: str, page_context: str,
                                             image_bytes: bytes | None) -> dict[str, Any]:
        """Nemotron VL looks at the actual product photo and drafts 3 alt-text candidates."""
        m_id = get_role_model("ASSIST")
        prompt = (f"Draft exactly 3 concise, specific English alt text candidates (each 4 to 12 words) for image "
                  f"#{image_id} ({image_src}) on a Seoul knitwear store ('Bukchon Wool'). Context around image: "
                  f"{page_context}. Describe what is actually visible in the photo. Never use generic words like "
                  f"'image', 'photo', 'picture', 'icon', 'banner', or filenames. Return ONLY a JSON array of 3 strings.")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image_bytes:
            content.insert(0, {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")}})
        try:
            resp = await asyncio.to_thread(_chat, {"model": m_id, "messages": [{"role": "user", "content": content}],
                                                   "max_tokens": 4096, "temperature": 0.4}, 45)
            usage = self.record_usage(m_id, resp.get("usage"))
            m = re.search(r"\[.*\]", _text(resp), re.S)
            arr = json.loads(m.group(0)) if m else []
            if isinstance(arr, list) and len(arr) >= 3:
                return {"image_id": image_id, "image_src": image_src, "candidates": [str(x).strip() for x in arr[:3]],
                        "model": m_id, "is_fallback": False, "usage": usage}
        except Exception:
            pass
        return {"image_id": image_id, "image_src": image_src, "model": m_id, "is_fallback": True, "usage": {},
                "candidates": [f"{page_context} product view", f"Bukchon Wool {page_context.lower()}",
                               f"Studio photograph of {page_context.lower()}"]}

    async def run_wording_guard(self, diff_text: str) -> dict[str, Any]:
        m_id = get_role_model("ROUTER")
        try:
            resp = await asyncio.to_thread(_chat, {"model": m_id, "max_tokens": 2048, "temperature": 0, "messages": [{
                "role": "user", "content": "Review this accessibility patch diff. List any visible wording or label changes "
                "that a human editor should verify. Return a JSON array of short strings (max 2 items).\n\n" + diff_text[:3000]}]}, 30)
            usage = self.record_usage(m_id, resp.get("usage"))
            m = re.search(r"\[.*\]", _text(resp), re.S)
            items = json.loads(m.group(0)) if m else []
            return {"model": m_id, "items": [str(x) for x in items if str(x).strip()][:2], "usage": usage}
        except Exception:
            return {"model": m_id, "items": [], "usage": {}}

    async def synthesize_tts(self, text: str) -> dict[str, Any]:
        # ponytail: no TTS model on Token Factory — the browser speaks it (Web Speech API); empty audio tells the UI so
        return {"model": "browser-web-speech", "text": text, "mime_type": "", "audio_data_url": "",
                "elapsed_s": 0.0, "usage": {}}

    async def diagnose_aviato(self, *, axe_summary: dict[str, Any], soundcheck_findings: list[dict[str, Any]],
                              css_excerpt: str) -> dict[str, Any]:
        m_id = get_role_model("REVIEW")
        t0 = time.time()
        prompt = (
            "You are diagnosing a real-world third-party Bootstrap e-commerce template (Themefisher Aviato, 5 pages).\n"
            "Analyze both the axe-core violations AND the three 'Passes axe — flagged by Placebo' findings below, plus the shared CSS excerpt.\n"
            "Return ONLY a JSON object with keys:\n- `headline`: 1-sentence executive summary\n"
            "- `shared_root_causes`: array of {title, files, axe_impact, soundcheck_catch, fix_strategy}\n"
            "- `why_axe_alone_is_not_enough`: 2-sentence explanation referencing the 3 Placebo findings.\n\n"
            f"AXE SUMMARY:\n{json.dumps(axe_summary, indent=2)}\n\nPLACEBO DETERMINISTIC FINDINGS:\n"
            f"{json.dumps(soundcheck_findings, indent=2)}\n\nSHARED CSS EXCERPT (css/style.css):\n{css_excerpt[:4000]}")
        async with NEMOTRON_SEMAPHORE:
            resp = await asyncio.to_thread(_chat, {"model": m_id, "messages": [{"role": "user", "content": prompt}],
                                                   "max_tokens": 16384, "temperature": 0.2}, 90)
        usage = self.record_usage(m_id, resp.get("usage"))
        txt = _text(resp)
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            parsed = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            parsed = {}
        parsed = parsed or {"headline": txt[:300], "shared_root_causes": [], "why_axe_alone_is_not_enough": ""}
        return {"model": m_id, "diagnosis": parsed, "elapsed_s": round(time.time() - t0, 2), "usage": usage}
