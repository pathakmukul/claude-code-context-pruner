"""Synthetic-payload sanity check for context_pruner.

Builds a fake conversation containing a mix of pruneable (denylisted) and
content-bearing tool calls, runs the prune logic against it, and asserts:

  - tool calls inside the sliding window are preserved (regardless of name)
  - denylisted tool calls outside the window are elided
  - non-denylisted tool calls outside the window are preserved

No network calls. Run with: python3 test_context_pruner.py
"""
import json
import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "pruner",
    os.path.join(HERE, "context_pruner.py"),
)
pruner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("mitmproxy", type(sys)("mitmproxy"))
mock_http = type(sys)("mitmproxy.http")
mock_http.HTTPFlow = type("HTTPFlow", (), {})
sys.modules.setdefault("mitmproxy.http", mock_http)
sys.modules.setdefault("mitmproxy.ctx", type(sys)("mitmproxy.ctx"))
spec.loader.exec_module(pruner)


def turn_user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def turn_assistant_chrome(turn_id, tool_name, result_text="OK"):
    tu_id = f"toolu_{turn_id}"
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"thinking turn {turn_id}"},
                {"type": "tool_use", "id": tu_id, "name": tool_name, "input": {"url": "https://example.com"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tu_id, "content": result_text}],
        },
    ]


messages = [turn_user("start")]
for i, tool in enumerate([
    "mcp__claude-in-chrome__navigate",          # PRUNE
    "mcp__claude-in-chrome__read_page",         # KEEP (content read)
    "mcp__claude-in-chrome__javascript_tool",   # KEEP (used for content extraction)
    "mcp__claude-in-chrome__tabs_create_mcp",   # PRUNE
    "mcp__claude-in-chrome__navigate",          # PRUNE
    "mcp__claude-in-chrome__read_page",         # KEEP
    "mcp__claude-in-chrome__javascript_tool",   # KEEP (within window)
    "mcp__claude-in-chrome__navigate",          # within window — should NOT be pruned
]):
    messages.extend(turn_assistant_chrome(i, tool, result_text=f"result-{i}"))

asst_indices = pruner._assistant_turn_indices(messages)
print(f"Total messages: {len(messages)}")
print(f"Assistant turns: {len(asst_indices)} | KEEP_RECENT_TURNS={pruner.config.keep_recent_turns}")

cutoff = asst_indices[-pruner.config.keep_recent_turns]
print(f"Cutoff index: {cutoff}")
prune_ids = pruner._collect_pruneable_ids(messages, cutoff)
print(f"Pruneable tool_use_ids (older than window, in denylist): {sorted(prune_ids)}")

count, samples = pruner._apply_prune(messages, prune_ids, dry_run=False)
print(f"Elided blocks: {count}")
print(f"Sample tool names: {samples}")
print()

# in-window navigate (latest) should be UNTOUCHED
last_nav_block = messages[-2]["content"][1]
assert last_nav_block["input"] != {"_elided": True}, "in-window navigate was wrongly elided"
print(f"OK in-window navigate preserved: input={last_nav_block['input']}")

# out-of-window navigate (oldest) should be ELIDED
first_nav_block = messages[1]["content"][1]
assert first_nav_block["input"] == {"_elided": True}, "out-of-window navigate not elided"
print(f"OK out-of-window navigate elided: input={first_nav_block['input']}")

# read_page out of window should be UNTOUCHED (not denylisted)
read_page_block = messages[3]["content"][1]
assert read_page_block["input"] != {"_elided": True}, "read_page was wrongly elided"
print(f"OK read_page preserved (not in denylist): input={read_page_block['input']}")

# javascript_tool out of window should also be UNTOUCHED
js_block = messages[5]["content"][1]
assert js_block["input"] != {"_elided": True}, "javascript_tool was wrongly elided"
print(f"OK javascript_tool preserved: input={js_block['input']}")

print("\nAll assertions passed.")
