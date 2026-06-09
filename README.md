# claude-proxy

A lightweight proxy for Claude Code that sits between Claude Code (CC) and the Anthropic API, intercepting and transforming requests before they reach the upstream endpoint.

## What it does

| Feature | Description |
|---|---|
| **Strip thinking blocks** | Removes all `thinking` blocks from assistant messages to reduce token usage |
| **Cache breakpoint injection** | Places an explicit `cache_control` marker on the second-to-last user message, so the API caches the conversation prefix |
| **System prompt injection** | Replaces CC's default system prompt with a custom one loaded from local files |
| **User style injection** | Appends a `<user_style>` block to the last user message for personality/tone control |
| **System reminder stripping** | Removes `<system-reminder>` tags from user messages to reclaim token budget |
| **OAuth passthrough** | Reads credentials from `~/.claude/.credentials.json` so CC subscription auth works through the proxy |

## How caching works

Claude's API supports [prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) with explicit cache breakpoints. The proxy injects a breakpoint on the **second-to-last user message** — this means everything up to that point is cached on subsequent requests, and you only pay full price for the final exchange.

Combined with full thinking-block stripping, this significantly reduces input tokens per turn in long conversations.

## Setup

**Requirements**: Python 3.10+, `fastapi`, `httpx`, `uvicorn`

```bash
pip install fastapi httpx uvicorn
```

### System prompt

The proxy replaces CC's default system prompt with your own. My approach: take Claude's web (claude.ai) system prompt as a base, strip most safety blocks and irrelevant sections, replace "user" with my actual name, rewrite overly rigid rules in a tone that fits both me and the AI's personality, and sprinkle in a few identity anchors. The result is noticeably better writing quality compared to the stock prompt.

**Configuration** — edit the paths at the top of `claude-proxy.py`:

```python
API_SYSTEM_PROMPT_PATH = "/home/ubuntu/api_system_prompt.md"   # custom system prompt
USER_STYLE_PATH = "/home/ubuntu/user_style.md"                 # user style block
CREDENTIALS_PATH = "~/.claude/.credentials.json"               # CC OAuth credentials
```

**Run**:

```bash
python claude-proxy.py
# Starts on 0.0.0.0:8090
```

**Point Claude Code at the proxy**:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8090
# or for remote:
export ANTHROPIC_BASE_URL=http://your-server:8090
```

## Request flow

```
Claude Code  →  proxy:8090/v1/messages  →  api.anthropic.com/v1/messages
                      │
                      ├─ strip system reminders
                      ├─ strip ALL thinking blocks
                      ├─ inject cache breakpoint (penultimate user msg)
                      ├─ replace system prompt
                      └─ inject user style
```

## Debug

The proxy dumps every request to `/tmp/proxy_dump_latest.json` and logs cache hit/miss stats to stdout:

```
[CACHE] read=128000 write=0 input=3200
```

## A note on thinking block strategies

The version in this repo strips **all** thinking blocks. This is the most aggressive approach — maximum token savings, best cache hit rate. **If full stripping works well for your use case (user style adherence stays solid, personality doesn't drift), you can ignore everything below.** The strategies listed here are only relevant if you notice degradation after stripping.

### The problem

Stripping thinking blocks at the proxy layer *can* degrade adherence to `user_style` and personality instructions (cause unclear — possibly related to how the model references its own prior reasoning). On the other hand, keeping all thinking blocks bloats the context and triggers CC's conversation compression much sooner.

If you'd rather not touch thinking block logic at all, tweaking your `user_style` writing or system prompt might also help — a more explicit or structured style prompt could compensate for the missing reasoning context. This is untested but worth trying before adding complexity to the proxy.

### If you use a session watcher

If you pair this proxy with a watcher that handles seamless session continuity, **full passthrough (no stripping)** is actually the simplest, cheapest, and most personality-stable approach. The watcher handles context overflow by forging new sessions, and thinking blocks get stripped naturally during the forge. One caveat: you'll want to increase the preserved raw token count (e.g. from 50k to 100k), since roughly half of a typical context is thinking blocks. The transition between sessions may feel more abrupt since the thinking style shifts — preserving the last few thinking blocks during forge might smooth this out, but this is untested.

### Selective stripping strategies I've tested

None of these are conclusively better than the others — listing them here for reference:

| Strategy | How it works | Pros | Cons |
|---|---|---|---|
| **Keep last N** | Preserve thinking blocks in the last ~5 messages, strip everything before, place cache breakpoint right before the preserved window | Simple, personality stays warm | The last 5 messages are always cache-write (not read), costs usage every turn |
| **Periodic flush** | Every ~50 messages, strip all thinking except the most recent ~10, let cache rebuild | Long stretches of cache hits between flushes | Full cache-write on every flush cycle (~every 50 turns) |
| **Keep every Nth** | Keep one thinking block every ~5 messages, strip the rest | Spreads reasoning anchors across context, decent cache rate | Compression still hits earlier than full-strip; unclear if sparse thinking blocks actually help |

The right choice depends on your conversation length, how much you rely on `user_style`, and whether you use a session watcher. If you find a clearly better strategy, please open an issue.

## License

MIT
