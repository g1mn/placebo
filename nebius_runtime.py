"""Placebo model runtime on Nebius Token Factory + NVIDIA Nemotron (OpenAI-compatible chat completions).

Drop-in replacement for gemini_agent.GeminiRuntime: same public methods and return shapes, so main.py and the
deterministic Guard (guard.py) are unchanged. The Gemini Interactions API kept conversation state server-side
(previous_interaction_id); here we keep the message history client-side and hand out our own interaction ids.

Roles (override with MODEL_<ROLE> env vars):
  FIX / REVIEW  nvidia/nemotron-3-super-120b-a12b   root-cause diagnosis + whole-file patch via tool calls
  ASSIST        nvidia/Nemotron-3.5-Super-VL         sees the product photo, drafts alt-text candidates
  ROUTER        nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B wording guard (can only add items to the human review list)
Scene C readout is voiced in the browser (Web Speech API), so there is no server TTS model.
"""
from __future__ import annotations

import asyncio
import base64
import json
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
SPEND_CAP_USD = float(os.environ.get("SPEND_CAP_USD", "10"))

# ponytail: rough USD per 1M tokens for the on-screen spend meter only — Token Factory billing is the source of truth
PRICING_PER_1M = {
    "nvidia/nemotron-3-super-120b-a12b": {"input": 0.30, "output": 0.90},
    "nvidia/Nemotron-3-Ultra-550b-a55b": {"input": 1.00, "output": 3.00},
    "nvidia/Nemotron-3.5-Super-VL": {"input": 0.30, "output": 0.90},
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {"input": 0.06, "output": 0.24},
    "nvidia/Nemotron-3_5-Lightning": {"input": 0.06, "output": 0.24},
}

DEFAULTS = {
    "FIX": "nvidia/nemotron-3-super-120b-a12b",
    "REVIEW": "nvidia/nemotron-3-super-120b-a12b",
    "ASSIST": "nvidia/Nemotron-3.5-Super-VL",
    "ROUTER": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
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


def _chat(body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is not configured in environment.")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token Factory {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from e


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
        cost = in_tok / 1e6 * rates["input"] + out_tok / 1e6 * rates["output"]
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
    ) -> dict[str, Any]:
        if self.total_spend_usd >= SPEND_CAP_USD:
            return {"status": "handoff", "reason": "spend_cap_reached",
                    "message": f"Escalated to human: cumulative ${SPEND_CAP_USD:g} spend cap reached"}
        demo = is_demo_mode()
        cutoff_s = (40.0 if is_retry else 85.0) if demo else (60.0 if is_retry else 120.0)
        model_id = get_role_model("FIX")
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
        companion: list[dict[str, Any]] = []
        usage_info: dict[str, Any] = {}
        inter_id = prev_id
        for hop in range(1, 4):
            remaining = max(5.0, cutoff_s - (time.time() - t0))
            emit({"type": "model_start", "model": model_id, "is_fallback": False, "thinking_level": "medium",
                  "hop": hop, "elapsed_s": round(time.time() - t0, 1)})
            body = {"model": model_id, "messages": msgs, "tools": OPENAI_TOOLS, "tool_choice": "auto",
                    "max_tokens": 32768, "temperature": 0.2}
            resp = await asyncio.wait_for(asyncio.to_thread(_chat, body, remaining), timeout=remaining + 2)
            usage_info = self.record_usage(model_id, resp.get("usage"))
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
            patch, propose_id, tool_results = None, None, []
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                cid = c["id"]
                emit({"type": "tool_call_start", "model": model_id, "name": name, "elapsed_s": round(time.time() - t0, 1)})
                if name == "update":
                    emit({"type": "agent_update", "model": model_id, "message": args.get("message", "Working…"),
                          "elapsed_s": round(time.time() - t0, 1)})
                    r = {"type": "function_result", "name": name, "call_id": cid, "result": json.dumps({"status": "ok"})}
                    tool_results.append(r); companion.append(r)
                elif name == "submit_diagnosis":
                    diagnosis = {"group_id": args.get("group_id", ""), "mode": args.get("mode", "patch"),
                                 "root_cause": args.get("root_cause", ""), "shared_files": args.get("shared_files", []),
                                 "cannot_fix": bool(args.get("cannot_fix", False)),
                                 "escalation_reason": args.get("escalation_reason", "")}
                    emit({"type": "diagnosis", "model": model_id, "diagnosis": diagnosis, "elapsed_s": round(time.time() - t0, 1)})
                    r = {"type": "function_result", "name": name, "call_id": cid, "result": json.dumps({"status": "recorded"})}
                    tool_results.append(r); companion.append(r)
                elif name == "escalate_group":
                    return {"status": "escalated", "model": model_id, "is_fallback": False, "interaction_id": inter_id,
                            "diagnosis": diagnosis, "reason": args.get("reason", "Model escalated group to human"),
                            "cannot_fix": bool(args.get("cannot_fix", True)), "usage": usage_info,
                            "elapsed_s": round(time.time() - t0, 2)}
                elif name == "propose_patch":
                    patch = {"files": args.get("files", []), "targeted_keys": args.get("targeted_keys", []),
                             "summary": args.get("summary", "")}
                    propose_id = cid
                    emit({"type": "patch_proposed", "model": model_id, "summary": patch["summary"],
                          "files": [f.get("path") for f in patch["files"]], "elapsed_s": round(time.time() - t0, 1)})
                elif name == "read_file":
                    p = (args.get("path") or "").strip().lstrip("/")
                    tool_results.append({"type": "function_result", "name": name, "call_id": cid,
                                         "result": current_files.get(p, "ERROR: File not found")})
                elif name == "run_scan":
                    tool_results.append({"type": "function_result", "name": name, "call_id": cid,
                                         "result": json.dumps(scan_summary)})
            if diagnosis and (diagnosis.get("mode") == "guide" or diagnosis.get("cannot_fix")):
                return {"status": "escalated", "model": model_id, "is_fallback": False, "interaction_id": inter_id,
                        "diagnosis": diagnosis, "reason": diagnosis.get("escalation_reason") or "cannot_fix",
                        "cannot_fix": True, "usage": usage_info, "elapsed_s": round(time.time() - t0, 2)}
            if patch is not None:
                # the retry path sends companion + propose_patch results back as tool messages (main.py)
                return {"status": "patch_proposed", "model": model_id, "is_fallback": False, "thinking_level": "medium",
                        "interaction_id": inter_id, "propose_call_id": propose_id, "companion_results": companion,
                        "diagnosis": diagnosis, "patch": patch, "usage": usage_info,
                        "elapsed_s": round(time.time() - t0, 2)}
            for r in tool_results:
                msgs.append({"role": "tool", "tool_call_id": r["call_id"], "content": str(r["result"])})
            companion = []
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
