import json
import os
import glob
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse

app = FastAPI()

ANTHROPIC_BASE = "https://api.anthropic.com"

CC_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

API_SYSTEM_PROMPT_PATH = "/home/ubuntu/api_system_prompt.md"
CLAUDE_MD_PATH = "/opt/cc-project/CLAUDE.md"
MEMORY_DIR = "/home/ubuntu/.claude/projects/-opt-cc-project/memory"
USER_STYLE_PATH = "/home/ubuntu/user_style.md"
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")

REPLACE_RULES = []
EXTRA_SYSTEM_BLOCKS = []


def get_oauth_token():
    if os.path.exists(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            creds = json.load(f)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    return None


def strip_cache_ttl(obj):
    if isinstance(obj, dict):
        cc = obj.get("cache_control")
        if isinstance(cc, dict) and "ttl" in cc:
            del cc["ttl"]
        for v in obj.values():
            strip_cache_ttl(v)
    elif isinstance(obj, list):
        for item in obj:
            strip_cache_ttl(item)


def build_system_from_files():
    blocks = []

    blocks.append({"type": "text", "text": CC_SYSTEM_PREFIX})

    if os.path.exists(API_SYSTEM_PROMPT_PATH):
        with open(API_SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
            blocks.append({"type": "text", "text": f.read(), "cache_control": {"type": "ephemeral", "ttl": "1h"}})

    return blocks


def strip_system_reminders(body: dict) -> dict:
    msgs = body.get("messages", [])
    for i, m in enumerate(msgs):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            if "<system-reminder>" in content:
                import re
                cleaned = re.sub(r"<system-reminder>.*?</system-reminder>\s*", "", content, flags=re.DOTALL).strip()
                if cleaned:
                    msgs[i] = {**m, "content": cleaned}
        elif isinstance(content, list):
            filtered = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip().startswith("<system-reminder>") and text.strip().endswith("</system-reminder>"):
                        continue
                filtered.append(block)
            if filtered:
                msgs[i] = {**m, "content": filtered}
    return body


def strip_thinking_blocks(body: dict) -> dict:
    msgs = body.get("messages", [])
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        filtered = [b for b in content if not (isinstance(b, dict) and b.get("type") == "thinking")]
        if filtered != content:
            msgs[i] = {**m, "content": filtered}
    return body


def inject_cache_breakpoint(body: dict, from_end=2) -> dict:
    msgs = body.get("messages", [])
    user_indices = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if len(user_indices) < from_end:
        return body
    target_idx = user_indices[-from_end]
    m = msgs[target_idx]
    content = m.get("content")
    if isinstance(content, list) and content:
        content[-1] = dict(content[-1])
        content[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    elif isinstance(content, str):
        msgs[target_idx] = {**m, "content": [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ]}
    return body

def inject_system(body: dict) -> dict:
    system = build_system_from_files()

    if REPLACE_RULES:
        for block in system:
            if block.get("type") == "text":
                for find, replace in REPLACE_RULES:
                    block["text"] = block["text"].replace(find, replace)

    if EXTRA_SYSTEM_BLOCKS:
        system = system + EXTRA_SYSTEM_BLOCKS

    body["system"] = system
    return body


def inject_user_style(body: dict) -> dict:
    if not os.path.exists(USER_STYLE_PATH):
        return body
    with open(USER_STYLE_PATH, encoding="utf-8") as f:
        style = f.read().strip()
    if not style:
        return body

    style_block = {"type": "text", "text": f"<user_style>\n{style}\n</user_style>"}

    # 只看最后一条 user message，不往前找
    msgs = body.get("messages", [])
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") != "user":
            continue
        content = msgs[i].get("content", "")
        if isinstance(content, str):
            msgs[i] = {**msgs[i], "content": f"<user_style>\n{style}\n</user_style>\n\n{content}"}
        elif isinstance(content, list):
            # 纯 tool_result → 不注入
            if all(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
                break
            # 有 text → 插到 tool_result 后面
            insert_pos = 0
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    insert_pos = j + 1
            msgs[i] = {**msgs[i], "content": content[:insert_pos] + [style_block] + content[insert_pos:]}
        break
    return body


def build_auth_headers(request: Request) -> dict:
    headers = {
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
    }

    auth = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")

    if auth:
        headers["Authorization"] = auth
    elif api_key:
        headers["x-api-key"] = api_key
    else:
        token = get_oauth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    for k, v in request.headers.items():
        if k.startswith("anthropic-") and k not in headers:
            headers[k] = v

    return headers


@app.get("/")
async def chat_page():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/system-info")
async def system_info():
    claude_md_ok = os.path.exists(CLAUDE_MD_PATH)
    mem_count = len([f for f in glob.glob(os.path.join(MEMORY_DIR, "*.md"))
                     if os.path.basename(f) != "MEMORY.md"])
    has_oauth = get_oauth_token() is not None
    return JSONResponse({
        "claude_md_loaded": claude_md_ok,
        "memory_count": mem_count,
        "replace_rules": len(REPLACE_RULES),
        "extra_blocks": len(EXTRA_SYSTEM_BLOCKS),
        "auth": "subscription" if has_oauth else "api_key_required",
    })


@app.get("/api/system-debug")
async def system_debug():
    dummy = {}
    dummy = inject_system(dummy)
    return JSONResponse(dummy["system"])


@app.api_route("/v1/messages", methods=["POST"])
async def proxy_messages(request: Request):
    raw_body = await request.body()
    body = json.loads(raw_body)

    # debug: dump last user msg structure
    msgs = body.get("messages", [])
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            import time
            with open(f"/tmp/last_user_msg_{int(time.time())}.json", "w") as df:
                c = msgs[i].get("content", "")
                json.dump({"index": i, "total": len(msgs), "content_type": type(c).__name__, "content_preview": str(c)[:500]}, df, indent=2, ensure_ascii=False)
            break

    # dump CC原始请求（inject之前）
    import time as _t
    with open('/tmp/proxy_dump_raw.json', 'w', encoding='utf-8') as _df:
        json.dump(body, _df, indent=2, ensure_ascii=False)

    strip_system_reminders(body)
    strip_thinking_blocks(body)
    inject_cache_breakpoint(body)
    body = inject_system(body)
    body = inject_user_style(body)
    # strip_cache_ttl(body)  # 保留CC的1h TTL

    # dump最终发给Anthropic的请求
    _dump_path = f'/tmp/proxy_dump_{int(_t.time())}.json'
    with open(_dump_path, 'w', encoding='utf-8') as _df:
        json.dump(body, _df, indent=2, ensure_ascii=False)
    with open('/tmp/proxy_dump_latest.json', 'w', encoding='utf-8') as _df:
        json.dump(body, _df, indent=2, ensure_ascii=False)
    print(f'[DUMP] {_dump_path}', flush=True)

    headers = build_auth_headers(request)

    is_stream = body.get("stream", False)

    async with httpx.AsyncClient(timeout=300) as client:
        if is_stream:
            req = client.build_request(
                "POST",
                f"{ANTHROPIC_BASE}/v1/messages",
                content=json.dumps(body),
                headers=headers,
            )
            resp = await client.send(req, stream=True)

            async def generate():
                try:
                    async for chunk in resp.aiter_bytes():
                        try:
                            for line in chunk.decode("utf-8", errors="ignore").split("\n"):
                                if line.startswith("data: ") and "cache" in line:
                                    evt = json.loads(line[6:])
                                    usage = evt.get("message", {}).get("usage") or evt.get("usage")
                                    if usage:
                                        cr = usage.get("cache_read_input_tokens", 0)
                                        cw = usage.get("cache_creation_input_tokens", 0)
                                        inp = usage.get("input_tokens", 0)
                                        print(f"[CACHE] read={cr} write={cw} input={inp}", flush=True)
                        except Exception:
                            pass
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                generate(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )
        else:
            resp = await client.post(
                f"{ANTHROPIC_BASE}/v1/messages",
                content=json.dumps(body),
                headers=headers,
            )
            return StreamingResponse(
                content=iter([resp.content]),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_fallback(request: Request, path: str):
    raw_body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k != "host"}

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.request(
            method=request.method,
            url=f"{ANTHROPIC_BASE}/{path}",
            content=raw_body,
            headers=headers,
        )
        return StreamingResponse(
            content=iter([resp.content]),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
