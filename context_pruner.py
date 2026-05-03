"""
mitmproxy addon: prune chrome navigation/control noise from outbound
Anthropic /v1/messages payloads.

Strategy: sliding window. Keep chrome blocks from the most recent
KEEP_RECENT_TURNS assistant turns intact. In any older turn, elide
tool_use/tool_result blocks for tools in PRUNE_TOOLS (pure-noise control
ops). Tools NOT in PRUNE_TOOLS are always kept (read_page, get_page_text,
javascript_tool, console/network reads, find, tabs_context — these carry
content the agent reasons over).

Run:
  mitmdump -s context_pruner.py -p 8080
  # dry-run (logs what WOULD be elided, mutates nothing):
  mitmdump -s context_pruner.py --set chrome_pruner_dry_run=true -p 8080

Then in another terminal:
  HTTPS_PROXY=http://localhost:8080 claude
"""

import json
from mitmproxy import ctx, http

KEEP_RECENT_TURNS = 4

PRUNE_TOOLS = {
    "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__tabs_close_mcp",
    "mcp__claude-in-chrome__gif_creator",
    "mcp__claude-in-chrome__upload_image",
    "mcp__claude-in-chrome__file_upload",
    "mcp__claude-in-chrome__resize_window",
    "mcp__claude-in-chrome__shortcuts_execute",
    "mcp__claude-in-chrome__switch_browser",
    "mcp__claude-in-chrome__browser_batch",
    "mcp__claude-in-chrome__computer",
    "mcp__claude-in-chrome__form_input",
}

STUB = "[chrome control op elided - older than sliding window]"


def load(loader):
    loader.add_option(
        name="chrome_pruner_dry_run",
        typespec=bool,
        default=False,
        help="If true, log what would be pruned but don't mutate the request.",
    )
    loader.add_option(
        name="chrome_pruner_dump_next",
        typespec=bool,
        default=False,
        help="If true, dump before/after JSON of the NEXT eligible request to /tmp, then auto-disable.",
    )


def _assistant_turn_indices(messages: list) -> list:
    return [i for i, m in enumerate(messages) if m.get("role") == "assistant"]


def _collect_pruneable_ids(messages: list, cutoff_index: int) -> set:
    """Tool_use IDs (in messages[< cutoff_index]) that should be pruned."""
    ids = set()
    for msg in messages[:cutoff_index]:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("name") in PRUNE_TOOLS:
                    ids.add(block.get("id"))
    return ids


def _apply_prune(messages: list, prune_ids: set, dry_run: bool) -> tuple[int, list]:
    """Returns (count_elided, sample_descriptions)."""
    count = 0
    samples = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use" and block.get("id") in prune_ids:
                if len(samples) < 5:
                    samples.append(f"tool_use {block.get('name')}")
                if not dry_run:
                    block["input"] = {"_elided": True}
                count += 1
            elif btype == "tool_result" and block.get("tool_use_id") in prune_ids:
                if not dry_run:
                    block["content"] = STUB
                count += 1
    return count, samples


def request(flow: http.HTTPFlow) -> None:
    if "api.anthropic.com" not in flow.request.pretty_host:
        return
    if not flow.request.path.startswith("/v1/messages"):
        return
    if flow.request.method != "POST":
        return

    try:
        body = json.loads(flow.request.get_text() or "{}")
    except json.JSONDecodeError:
        return

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    asst_indices = _assistant_turn_indices(messages)
    if len(asst_indices) <= KEEP_RECENT_TURNS:
        return  # not enough history to prune

    # Cutoff = first message belonging to the (KEEP_RECENT_TURNS)-th-from-last
    # assistant turn. Everything strictly before that index is fair game.
    cutoff_index = asst_indices[-KEEP_RECENT_TURNS]

    prune_ids = _collect_pruneable_ids(messages, cutoff_index)
    if not prune_ids:
        return

    dry_run = ctx.options.chrome_pruner_dry_run
    dump_next = ctx.options.chrome_pruner_dump_next

    # snapshot BEFORE mutation for diff
    if dump_next:
        with open("/tmp/chrome-pruner-before.json", "w") as f:
            json.dump(body, f, indent=2)

    count, samples = _apply_prune(messages, prune_ids, dry_run)
    if count == 0:
        return

    if not dry_run:
        body["messages"] = messages
        flow.request.set_text(json.dumps(body))

    if dump_next:
        with open("/tmp/chrome-pruner-after.json", "w") as f:
            json.dump(body, f, indent=2)
        ctx.options.chrome_pruner_dump_next = False
        ctx.log.info("[chrome-pruner] DUMPED before/after to /tmp/chrome-pruner-{before,after}.json (auto-disabled)")

    tag = "DRY-RUN would elide" if dry_run else "elided"
    sample_str = ", ".join(samples[:3]) if samples else "n/a"
    ctx.log.info(
        f"[chrome-pruner] {tag} {count} blocks | "
        f"{len(messages)} msgs, {len(asst_indices)} asst turns, "
        f"window={KEEP_RECENT_TURNS} | sample: {sample_str}"
    )
