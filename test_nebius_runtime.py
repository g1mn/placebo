"""Offline self-check for nebius_runtime (no network): python test_nebius_runtime.py"""
import asyncio
import json

import nebius_runtime as nr


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def reply(*calls):
    return {"choices": [{"message": {"content": "", "tool_calls": list(calls)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 50}}}


def check_pairing(messages):
    """Every assistant tool_call id must be answered by a tool message before the next non-tool message."""
    open_ids = set()
    for m in messages:
        if m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, f"reply to unknown id {m['tool_call_id']}"
            open_ids.discard(m["tool_call_id"])
            continue
        assert not open_ids, f"unanswered tool_call ids {open_ids}"
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}


def run(script, retry_script=None):
    sent = []
    queue = list(script)

    def fake_chat(body, timeout_s):
        check_pairing(body["messages"])
        sent.append(body)
        return queue.pop(0)

    nr._chat = fake_chat
    rt = nr.NebiusRuntime()
    kw = dict(previous_interaction_id=None, is_retry=False, current_files={"a.css": "x"},
              baseline_scan_summary={"total_violations": 1})
    res = asyncio.run(rt.run_argon_turn(input_payload="fix it", **kw))
    if retry_script:  # what main.py sends on a retry: companion results + feedback for the propose call
        queue.extend(retry_script)
        payload = list(res["companion_results"]) + [{"type": "function_result", "name": "propose_patch",
                                                     "call_id": res["propose_call_id"], "result": "{}"}]
        res2 = asyncio.run(rt.run_argon_turn(input_payload=payload, **{**kw, "previous_interaction_id": res["interaction_id"],
                                                                        "is_retry": True}, attempt=1))
        return res, res2, sent
    return res, sent


good = {"files": [{"path": "a.css", "content": "y"}], "targeted_keys": [{"rule": "r", "selector": "s"}], "summary": "ok"}
again = [reply(call("z1", "propose_patch", good))]

# 1. read_file + propose in one turn, then a retry: every id answered
res, res2, sent = run([reply(call("c0", "read_file", {"path": "a.css"}), call("c1", "propose_patch", good))], again)
assert res["status"] == res2["status"] == "patch_proposed"

# 2. two propose calls in one turn are merged, not overwritten
res, res2, sent = run([reply(call("c1", "propose_patch", {"files": [{"path": "a.css", "content": "1"}], "summary": "first"}),
                             call("c2", "propose_patch", {"files": [{"path": "b.html", "content": "2"}], "summary": "second"}))], again)
assert sorted(f["path"] for f in res["patch"]["files"]) == ["a.css", "b.html"] and res["propose_call_id"] == "c2"

# 3. malformed propose next to a valid one, plus an unknown tool name
res, res2, sent = run([reply(call("c1", "propose_patch", {"files": "[{oops"}), call("c2", "functions.read", {}),
                             call("c3", "propose_patch", good))], again)
assert res["propose_call_id"] == "c3"

# 4. Nemotron quirk: files string = complete array + trailing sibling arguments; and missing closing brace
trail = '{"files": "[{\\"path\\": \\"a.css\\", \\"content\\": \\"y\\"}], \\"targeted_keys\\": [{\\"rule\\""}'
brace = '{"files": "[{\\"path\\": \\"a.css\\", \\"content\\": \\"y\\"]"}'
stray = '{"files": "[{\\"path\\": \\"a.css\\", \\"content\\": \\"y\\"]}]"}'
escape = '{"files": "[{\\"path\\": \\"a.css\\", \\"content\\": \\"<p>\\\\    <i>\\"}]"}'
for args in (trail, brace, stray, escape):
    res, sent = run([reply(call("c1", "propose_patch", args))])
    assert [f["path"] for f in res["patch"]["files"]] == ["a.css"], res

# 5. unparseable files: rejected in-conversation, model asked again on the next hop, usage summed over hops
res, sent = run([reply(call("c1", "propose_patch", {"files": "[{nope"})), reply(call("c2", "propose_patch", good))])
assert res["status"] == "patch_proposed" and len(sent) == 2 and res["usage"]["input_tokens"] == 200

# 6. items without a path or content are dropped; scalar targeted_keys do not crash
assert nr._patch_files([{"content": "x"}, {"path": "a"}, {"path": "b", "content": "y"}]) == [{"path": "b", "content": "y"}]
res, sent = run([reply(call("c1", "propose_patch", {**good, "targeted_keys": 5}))])
assert res["patch"]["targeted_keys"] == []

print("ok")
