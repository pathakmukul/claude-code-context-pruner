"""
mitmproxy addon: prune noisy tool control ops from outbound
Anthropic /v1/messages payloads.

Strategy: sliding window. Keep blocks from the most recent
KEEP_RECENT_TURNS assistant turns intact. In any older turn, elide
tool_use/tool_result blocks for tools in PRUNE_TOOLS (pure-noise control
ops). Tools NOT in PRUNE_TOOLS are always kept.

Run:
  mitmdump -s context_pruner.py -p 8080
  # dry-run (logs what WOULD be elided, mutates nothing):
  mitmdump -s context_pruner.py --set context_pruner_dry_run=true -p 8080
  # with a custom config file:
  mitmdump -s context_pruner.py --set context_pruner_config=config.json -p 8080

Then in another terminal:
  HTTPS_PROXY=http://localhost:8080 claude
"""

import json
import os
from mitmproxy import ctx, http

# Default configuration (Chrome agent example)
DEFAULT_KEEP_RECENT_TURNS = 4

DEFAULT_PRUNE_TOOLS = {
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

DEFAULT_STUB = "[control op elided - older than sliding window]"

class ConfigLoader:
    def __init__(self):
        self.keep_recent_turns = DEFAULT_KEEP_RECENT_TURNS
        self.prune_tools = DEFAULT_PRUNE_TOOLS
        self.stub = DEFAULT_STUB
        self._last_config_path = None
        self._last_mtime = 0

    def load_if_needed(self, config_path):
        if not config_path:
            # Fall back to defaults
            self.keep_recent_turns = DEFAULT_KEEP_RECENT_TURNS
            self.prune_tools = DEFAULT_PRUNE_TOOLS
            self.stub = DEFAULT_STUB
            return

        try:
            mtime = os.path.getmtime(config_path)
            if self._last_config_path == config_path and self._last_mtime == mtime:
                return # No change
            
            with open(config_path, "r") as f:
                data = json.load(f)
                
            self.keep_recent_turns = data.get("keep_recent_turns", DEFAULT_KEEP_RECENT_TURNS)
            self.prune_tools = set(data.get("prune_tools", DEFAULT_PRUNE_TOOLS))
            self.stub = data.get("stub", DEFAULT_STUB)
            
            self._last_config_path = config_path
            self._last_mtime = mtime
            ctx.log.info(f"[context-pruner] Loaded config from {config_path}")
        except Exception as e:
            ctx.log.error(f"[context-pruner] Error loading config {config_path}: {e}")

config = ConfigLoader()

def load(loader):
    loader.add_option(
        name="context_pruner_config",
        typespec=str,
        default="",
        help="Path to JSON config file to override KEEP_RECENT_TURNS, PRUNE_TOOLS, and STUB.",
    )
    loader.add_option(
        name="context_pruner_dry_run",
        typespec=bool,
        default=False,
        help="If true, log what would be pruned but don't mutate the request.",
    )
    loader.add_option(
        name="context_pruner_dump_next",
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
                if block.get("name") in config.prune_tools:
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
                    block["content"] = config.stub
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

    config.load_if_needed(ctx.options.context_pruner_config)

    asst_indices = _assistant_turn_indices(messages)
    if len(asst_indices) <= config.keep_recent_turns:
        return  # not enough history to prune

    # Cutoff = first message belonging to the (KEEP_RECENT_TURNS)-th-from-last
    # assistant turn. Everything strictly before that index is fair game.
    cutoff_index = asst_indices[-config.keep_recent_turns]

    prune_ids = _collect_pruneable_ids(messages, cutoff_index)
    if not prune_ids:
        return

    dry_run = ctx.options.context_pruner_dry_run
    dump_next = ctx.options.context_pruner_dump_next

    # snapshot BEFORE mutation for diff
    if dump_next:
        with open("/tmp/context-pruner-before.json", "w") as f:
            json.dump(body, f, indent=2)

    count, samples = _apply_prune(messages, prune_ids, dry_run)
    if count == 0:
        return

    if not dry_run:
        body["messages"] = messages
        flow.request.set_text(json.dumps(body))

    if dump_next:
        with open("/tmp/context-pruner-after.json", "w") as f:
            json.dump(body, f, indent=2)
        ctx.options.context_pruner_dump_next = False
        ctx.log.info("[context-pruner] DUMPED before/after to /tmp/context-pruner-{before,after}.json (auto-disabled)")

    tag = "DRY-RUN would elide" if dry_run else "elided"
    sample_str = ", ".join(samples[:3]) if samples else "n/a"
    ctx.log.info(
        f"[context-pruner] {tag} {count} blocks | "
        f"{len(messages)} msgs, {len(asst_indices)} asst turns, "
        f"window={config.keep_recent_turns} | sample: {sample_str}"
    )
