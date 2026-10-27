# Feedback: Nebius Token Factory and NVIDIA Nemotron

Notes from porting Placebo from Gemini to Nemotron on Token Factory, written as things happened (from Oct 27, 2026, KST). Every item below happened in this repo, and each fix names the file it lives in.

## What we used

| Role in Placebo | Model | Why |
|---|---|---|
| Diagnose and patch a root-cause group (tool calls) | `nvidia/nemotron-3-super-120b-a12b` | Long whole-file outputs at $0.30 / $0.90 per 1M tokens |
| Second retry only | `nvidia/Nemotron-3-Ultra-550b-a55b` | Escalation when Super fails twice |
| Look at each product photo, draft 3 alt-text candidates | `nvidia/Nemotron-3.5-Super-VL` | Needs pixels, not filenames |
| Flag wording changes for the human reviewer | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Cheap, runs on every diff |

## What worked well

- **OpenAI-compatible chat completions with tools.** Porting the model layer took one file ([`nebius_runtime.py`](nebius_runtime.py), about 300 lines at first). We replaced Gemini's server-side conversation state with a client-side message history.
- **`/v1/models?verbose=true` returns prices.** We checked our cost table against it, so the cost receipt in the UI uses the published rates rather than numbers we typed by hand.
- **Onboarding.** We signed up as an individual from Korea, and a Korean card was accepted. The $1 trial credit was enough to smoke-test every model before we spent anything.
- **Credits arrived the same day.** The AI Builder Program welcome mail (Oct 27, 13:38 KST) said a $25 Token Factory credit was "coming your way in a separate email". A promo-code mail followed at 16:54 KST. Once applied, the code showed up in Transactions as "Adjustment: AI Builder Program Credits +$25.00".
- **Cheap enough to measure properly.** At 08:15 UTC on Oct 27, Transactions showed $2.40 of consumption for the month so far. That was mostly a day of benchmarking: dozens of end-to-end and ablation runs on Super, plus Ultra, Super-VL and Nano. Both Usage and Transactions may lag (see 4), so the final figure will be higher.

## What broke, and what we did

### 1. Array-typed tool arguments sometimes arrive as a broken string (Super 120B)

`propose_patch` takes `files: array<{path, content}>`. Sometimes `files` came back as a JSON **string** rather than an array. On Oct 27 we captured 27 such strings across our benchmark runs, in four shapes:

- **The complete array followed by the remaining arguments**, e.g. `[{"path": "css/tokens.css", "content": "…"}], "targeted_keys": [{"rule": "color-contrast", …`. The parameter seems to have been closed late, either by the model or by the tool-call parser.
- **The last object missing its closing brace**: `[{"path": "css/styles.css", "content": "…}\n"]`.
- **A stray `]` after a string value**: `…}\n"], "path": "css/styles.css"}]` or `…"]}]`.
- **A bare backslash where a newline was meant**: `</div>\    <div class=…` (invalid JSON escape).

How often: in our final 5 end-to-end runs, 4 of 25 `propose_patch` calls (16%) arrived as a malformed string. All four belonged to the color-contrast group, which writes the two largest files, about 5.8–6.6 KB of CSS inside one string. 26 of the 27 captured strings were 5 KB or more.

Our fix is `_loads` plus `_repair` in [`nebius_runtime.py`](nebius_runtime.py), covered by [`test_nebius_runtime.py`](test_nebius_runtime.py). They keep the first complete JSON value and fix only these four shapes, and only for text that starts with `[` or `{`. All 27 captured cases now parse. If a string still fails, we send a tool error back in the same conversation instead of staging an empty patch. Before the fix, the color-contrast group went to a human in 4 of 8 end-to-end runs. After it, the group passed on the first try in 5 of 5.

**Ask:** a strict or guided-decoding option for tool arguments, or documentation saying which tool-call parser each model uses. `GET /v1/models?verbose=true` lists `supported_features: ["tools", "reasoning"]` for every Nemotron model, and no structured-output feature.

### 2. Transient TLS timeouts and overload errors

- On Oct 27 at about 14:00 KST we saw two `The handshake operation timed out` errors within roughly 10 minutes, one on `/v1/models` and one on `/v1/chat/completions`. Other HTTPS hosts were fine during that window, and the next requests went through.
- Between about 14:21 and 14:25 KST, six `/v1/chat/completions` calls to Super 120B returned `529 {"error":{"type":"server_error","message":"System is overloaded"}}`. Our runtime did not retry yet, so each one sent a whole fix group to a human.

We now resend in two cases only, both within the request's original deadline (`_chat` in [`nebius_runtime.py`](nebius_runtime.py)). Connection-level failures are retried because the request never reached the model. `429`, `503` and `529` responses are retried with backoff because they arrive before any work is done. **Ask:** document whether 529 responses carry `Retry-After` and whether overload responses are billed. We honor `Retry-After` when it is present.

### 3. No speech model in our catalog

Placebo plays a before/after screen-reader readout. On Gemini we used a TTS model. On Oct 27, `GET /v1/models?verbose=true` listed 30 models whose modalities were only `text->text`, `text+image->text` and `text->embedding`. So the readout is voiced in the browser with the Web Speech API and labeled "synthesized readout".

### 4. Two small console papercuts (Oct 27)

- **Top up is an icon.** The docs say "click your balance, then Top up". In the console the control is a `+` icon next to the balance, and its "Top up" text is visually hidden. Screen readers get the name, but a sighted user looking for the word "Top up" will not find it.
- **Usage lags.** At about 08:15 UTC the Usage page still said "Last updated: 06:18, UTC" and showed $2.20. Transactions already showed $2.40. When you are watching a hackathon budget, a two-hour lag matters.
