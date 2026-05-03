# claude-code-context-pruner

A wire-level context-pruning framework for Claude Code. Strips deterministic noise from outbound API payloads so long-running agents stop drowning in their own tool-call history — without touching the local transcript and without spending an LLM call on `/compact`.

Works for **any** Claude Code agent, with **any** tool whose results don't need to live forever in context. Browser automation is the example walked through below because it's where the problem is most visible, but the same addon prunes anything you put in its denylist: file system tools, search results, MCP servers, custom tools, subagent outputs.

## The problem (generalized)

Claude Code keeps every tool result in the conversation context for the entire session. For most agents this is fine. For agents that:

- run for dozens or hundreds of turns,
- call high-frequency tools that return verbose-but-disposable results (navigation, polling, screenshots, search hits, large file dumps, repeated discovery scans),
- need to stay focused on a long-horizon task,

…this turns into **context rot**. The signal-to-noise ratio of the working context degrades as the session grows, and the agent's reasoning quality drops with it. Every turn also re-sends the entire bloated history to the API, paying the token cost on every call.

The standard fixes have known trade-offs:

- **`/compact`** — costs an LLM call, lossy, you don't control what survives, fires only when you trigger it.
- **Subagents** — solve it structurally for some workloads (delegate the noisy work to a child whose context dies on return), but don't help long-running single-agent loops.
- **Hooks (`PostToolUse` etc.)** — can react to a tool call but cannot rewrite the in-memory `messages[]` array or shrink a tool result already glued to context. Too late.
- **Reducing tool verbosity at the source** — only works if you control the tool. MCP servers and built-in tools are out of reach.

## The approach

A man-in-the-middle proxy between Claude Code and `api.anthropic.com`. On every outbound `/v1/messages` POST, it:

1. Walks the `messages[]` array
2. Identifies tool_use / tool_result blocks for tools listed in a configurable denylist
3. For any such block older than the most recent N assistant turns (sliding window, default 4), replaces its content with a tiny stub
4. Forwards the slimmed payload to Anthropic

The agent's local JSONL transcript is untouched — only the bytes on the wire change. The agent's running context is fed the leaner version on the next turn, so context rot stops compounding. No LLM calls. Deterministic. Reversible (kill the proxy, original behavior returns).

```
                        ┌─────────────────────────┐
                        │  mitmproxy (localhost)  │
   Claude Code  ────►   │   ┌───────────────┐     │  ────►  api.anthropic.com
   (any agent,          │   │ pruner addon  │     │
    any tool stack)     │   └───────────────┘     │
                        │   walks messages[],     │
                        │   stubs out blocks      │
                        │   matching denylist +   │
                        │   older than window     │
                        └─────────────────────────┘
```

The agent process never knows. The local transcript on disk shows the full unmuted history (useful for debugging). Only the API receives the slimmed version.

## Two knobs are all you need

```python
KEEP_RECENT_TURNS = 4

PRUNE_TOOLS = {
    "tool_name_1",
    "tool_name_2",
    ...
}
```

- `KEEP_RECENT_TURNS` — how many recent assistant turns of activity stay fully intact, regardless of denylist. Larger window = more context preserved, more cost. Smaller = more aggressive pruning.
- `PRUNE_TOOLS` — which tool names are eligible for pruning when out of window. **Tools NOT in this set are always preserved** (the conservative default — your content-bearing tools survive even if you forget to whitelist them).

Pick the tools you know are noisy-but-disposable for your agent. Leave content-bearing tools out.

## Picking what to prune (the principle)

For each tool your agent calls, ask: **"Two turns from now, does the agent need to see the full result of this call, or is the relevant takeaway already encoded in its subsequent reasoning text?"**

- If the answer is "the takeaway is in the text" → safe to prune
- If the answer is "the agent might need to re-read this result later" → keep

Examples of generally safe-to-prune patterns across any agent:

- Navigation / control ops (no return value matters once the action completes)
- Acknowledgments and status confirmations
- Large binary or media-bearing returns (screenshots, audio, PDFs)
- Polling/wait calls
- Repeated discovery scans (tab lists, directory listings) where only the latest matters

Generally NOT safe to prune:

- Anything that returned content the agent quoted, summarized, or made decisions from
- Search results the agent might want to refer back to
- File reads (especially partial reads — the agent may need other parts later)
- Errors or unexpected return values

## Worked example: pruning `claude-in-chrome` MCP noise

This is the agent that originally motivated the addon, and the example in `context_pruner.py`.

### Why chrome agents bleed context fast

A long-running browser-automation session, after a few dozen turns, looks like:

```
Turn 30 input to model:
  - 60% navigation/wait/click logs from turns 1-25
  - 25% screenshot acknowledgments
  - 10% tab-context blobs that repeat verbatim every turn
  -  5% the actual reasoning the agent needs to act on
```

The MCP server is well-behaved — it returns informative tool results. But "navigated to URL" + "tab context: tabId X, available tabs: ..." + a recurring `<system-reminder>` about `browser_batch` is a few hundred bytes per chrome action, multiplied by every turn, persisting forever.

### The denylist used

```python
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
```

These are all pure control ops — once the action completes, the result has no further consequence.

### What's deliberately kept off the denylist

Even though they're chrome tools, these return content the agent reasons over and stay in context regardless of age:

- `read_page` / `get_page_text` — actual page content
- `javascript_tool` — often used for content extraction via DOM queries
- `read_console_messages` / `read_network_requests` — debugging signal
- `find` — search results
- `tabs_context_mcp` — explicit discovery output

### Observed impact (chrome example)

In a long-running session with ~60 assistant turns of browser activity, the proxy was eliding 30-40 control-op blocks per request. Each elided tool_result was stripping 600-800 bytes. A single dumped payload showed a ~4% size reduction for the request as a whole, with the savings concentrated in older history (where the LLM's attention is most distractible anyway).

A surprising secondary win: the chrome MCP server emits a `<system-reminder>` in *every* tool_result reminding the agent to use `browser_batch`. Across 60 turns that's 60 copies of the same reminder accumulating in context. The pruner removes these for any out-of-window turn, which alone reclaims meaningful working memory.

### Adapting it to your agent

Replace `PRUNE_TOOLS` with the tool names from your stack. Tool names follow the format Claude Code uses internally — for MCP tools that's `mcp__<server>__<tool>`, for built-ins that's the bare name (`Bash`, `Read`, etc.). Find the exact names in your local JSONL transcript at `~/.claude/projects/<project>/<session>.jsonl` by grepping for `"name":` inside `tool_use` blocks.

That's the only change. The pruning logic, sliding window, and verification harness all stay the same.

## Files

- `context_pruner.py` — the mitmproxy addon. ~120 lines. Configured for the chrome example out of the box; edit `PRUNE_TOOLS` for your agent.
- `test_context_pruner.py` — synthetic-payload sanity check. Builds a fake conversation with a mix of pruneable and content-bearing tool calls, runs the prune logic, asserts the right things got elided and the right things stayed intact. No network calls.

## Setup

```bash
# 1. install mitmproxy
pipx install mitmproxy

# 2. boot mitmdump once to generate the CA cert
mitmdump &
sleep 3
kill %1

# 3. trust the cert (one-time, requires sudo on macOS)
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  ~/.mitmproxy/mitmproxy-ca-cert.pem

# 4. run the pruner
mitmdump -s context_pruner.py \
  --ignore-hosts '^(?!api\.anthropic\.com).*$' \
  -p 58473
```

In another terminal, route Claude Code through the proxy:

```bash
HTTPS_PROXY=http://localhost:58473 claude
```

### Important: `--ignore-hosts` and why you need it

`HTTPS_PROXY` routes **every** outbound HTTPS connection through the proxy — not just Anthropic API calls. By default, mitmproxy will TLS-terminate all of that traffic (substituting its own cert) which breaks any client that doesn't trust the mitmproxy CA. Notably, **Python's `ssl` module on macOS does not honor the system keychain** — it uses certifi's bundled CA list. So a Bash subprocess that calls a third-party API (cricket data, a search API, GitHub, etc.) through the proxy will fail with `CERTIFICATE_VERIFY_FAILED` even after you trust the cert in macOS.

The `--ignore-hosts` flag tells mitmproxy: "for any host matching this regex, don't decrypt — just shuttle the encrypted bytes blindly." The regex `^(?!api\.anthropic\.com).*$` matches everything *except* `api.anthropic.com`. Net effect:

- Anthropic traffic → still gets the full pruning treatment (decrypt → mutate → re-encrypt)
- All other HTTPS traffic → passes through end-to-end encrypted, untouched

You'll know it's working when a non-Anthropic HTTPS call goes through cleanly:

```bash
HTTPS_PROXY=http://localhost:58473 python3 -c \
  "import urllib.request; print(urllib.request.urlopen('https://api.github.com', timeout=5).status)"
# → 200
```

Without the flag, the same call returns `CERTIFICATE_VERIFY_FAILED`.

## Verifying it works

The local JSONL transcript will *not* show evidence of pruning — that's by design, and a common source of "is this thing on?" confusion. The proxy mutates the wire payload, not the local record.

To prove the API is receiving the leaner version:

```bash
# arm a one-shot dump on next eligible request
mitmdump -s context_pruner.py \
  --ignore-hosts '^(?!api\.anthropic\.com).*$' \
  --set chrome_pruner_dump_next=true \
  -p 58473

# trigger one turn through the agent, then:
diff <(jq -S . /tmp/chrome-pruner-before.json) \
     <(jq -S . /tmp/chrome-pruner-after.json) | head -50
```

You'll see chunks like:

```diff
<       "input": { "url": "https://example.com/...", "tabId": 12345 }
---
>       "input": { "_elided": true }

<       "content": [ { "text": "Navigated to ...", ... }, { "text": "Tab Context: ...", ... } ]
---
>       "content": "[control op elided - older than sliding window]"
```

### What the savings actually look like

A single-request size diff understates the real benefit. In one verified test session, a single ~190KB payload shrank by ~4% after pruning. That number is small because most of any payload's bytes are the system prompt + tool schemas + recent (in-window) turns, and those are intentionally untouched.

The real win is **compounding over a long-running session**, in two ways:

1. **Byte savings grow with session length.** Without the pruner, every tool result from turn 1 still sits in turn 100's context. With the pruner, anything denylisted dies after `KEEP_RECENT_TURNS`. By the time a session is dozens of turns deep, hundreds of stale tool_results have been zeroed out. Per-request savings climb from a few percent early on to substantially more late-session.

2. **Attention budget, not just token cost.** The model's effective focus on relevant context degrades when surrounded by repeated noise, even if that noise is technically cheap in tokens. Eliding the same recurring "navigated to URL" + "tab context: ..." blob 40+ times stops the model from re-reading dead history when it's trying to reason about the current step. This is the part that actually changes agent quality — and it doesn't show up in a single-request size diff at all.

A common secondary win: many MCP servers (the `claude-in-chrome` example included) emit a `<system-reminder>` in *every* tool_result. Across 60 turns that's 60 copies of the same instruction in context. Pruning out-of-window tool_results kills these duplicates outright.

### Squeezing more savings: shrink the stub

The default replacement stub is a short human-readable string:

```python
STUB = "[control op elided - older than sliding window]"
```

If you want to maximize byte savings, shorten it — even a single character works:

```python
STUB = "x"
```

The agent only ever sees the stub for *out-of-window* tool results, where you've already decided the content doesn't matter. There's no functional reason for it to be human-readable. The longer string in the default exists purely to make the diff inspection step (above) more legible to *you* during setup. Once you trust it, shrink the stub and reclaim those bytes too.

The same applies to the tool_use `input` replacement (`{"_elided": True}`) — could be `{}` if you want.

## Caveats

- macOS-specific cert-trust step shown above. Linux uses your distro's CA store; Windows uses certmgr.
- If Claude Code adds TLS certificate pinning in a future version, this approach stops working silently. Easy to detect (mitmdump logs handshake failures) but no workaround exists at that point.
- The proxy is per-machine, not per-session. Routes apply to any Claude Code process that has `HTTPS_PROXY` set to it.
- Wire-level mutation means the API truly does not see the elided content — this is permanent for that request. The local JSONL retains the original, so nothing is lost from your records.
- The default denylist in this repo targets the `claude-in-chrome` MCP server because that's the worked example. **If you don't change `PRUNE_TOOLS`, the addon does nothing useful for non-chrome agents** — you must add the tool names relevant to your stack.

## License

MIT.
