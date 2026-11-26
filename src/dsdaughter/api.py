"""HTTP 接口：健康检查、普通聊天、SSE 流式聊天、内置调试页。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from .agent import Agent
from .llm import LLMError
from .types import ChatResult

_MD = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")

_DOC_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}} · DSDaughter</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 15px/1.75 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f6f4f1; color: #2a2622; }
  nav { position: sticky; top: 0; background: #fbfaf8; border-bottom: 1px solid #e2ddd6;
        padding: 12px 18px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  nav b { font-weight: 600; }
  nav a { color: #2a2622; text-decoration: none; border-bottom: 1px dotted #b9b1a6; }
  nav a:hover { border-bottom-style: solid; }
  article { max-width: 900px; margin: 0 auto; padding: 28px 18px 80px; }
  article h1 { font-size: 26px; margin: 0 0 18px; }
  article h2 { font-size: 20px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #e8e3dc; }
  article h3 { font-size: 16px; margin: 24px 0 8px; }
  article p, article li { color: #3a3530; }
  article code { background: #efece7; border-radius: 4px; padding: 1px 5px; font-size: 13px; }
  article pre { background: #23201d; color: #f2efe9; padding: 14px 16px; border-radius: 10px;
                overflow-x: auto; font-size: 13px; line-height: 1.55; }
  article pre code { background: none; color: inherit; padding: 0; }
  article table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
  article th, article td { border: 1px solid #e2ddd6; padding: 7px 10px; text-align: left; vertical-align: top; }
  article th { background: #efece7; }
  article blockquote { margin: 12px 0; padding: 8px 14px; border-left: 3px solid #c9c1b6;
                       background: #fbfaf8; color: #6b645c; }
  article a { color: #2a2622; }
</style>
</head>
<body>
<nav>
  <b>DSDaughter</b>
  <a href="/">聊天</a>
  <a href="/project">项目结构</a>
  <a href="/plugins-guide">插件指南</a>
  <a href="/api/plugins">插件状态</a>
</nav>
<article>
{{BODY}}
</article>
</body>
</html>
"""

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _result_payload(result: ChatResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "citations": [c.to_dict() for c in result.citations],
        "finish_reason": result.finish_reason,
        "usage": result.usage,
    }


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="default", max_length=128)


class ChatResponse(BaseModel):
    reply: str
    citations: list[dict[str, str]]
    finish_reason: str
    usage: dict[str, int]


def create_router(agent: Agent) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ok",
            "persona": agent.persona.name,
            "model": agent.settings.model,
            "llm": type(agent.llm).__name__,
            "web_tools": agent.settings.web_tools,
            "thinking": agent.settings.thinking,
            "memory": type(agent.memory).__name__,
        }
        qq = getattr(request.app.state, "qq", None)
        if qq is not None:
            payload["qq"] = qq.status()
        return payload

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        try:
            result = await agent.reply(request.session_id, request.message)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return ChatResponse(
            reply=result.text,
            citations=[c.to_dict() for c in result.citations],
            finish_reason=result.finish_reason,
            usage=result.usage,
        )

    @router.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        async def event_source():
            thinking_buffer = ""

            async def flush_thinking():
                nonlocal thinking_buffer
                if thinking_buffer:
                    yield _sse("thinking", {"text": thinking_buffer})
                    thinking_buffer = ""

            try:
                async for event in agent.stream_reply(request.session_id, request.message):
                    if event.kind == "thinking":
                        # 思考是逐字下发的，攒一批再发，避免 SSE 事件爆炸
                        thinking_buffer += event.text
                        if len(thinking_buffer) >= 120:
                            async for chunk in flush_thinking():
                                yield chunk
                        continue
                    async for chunk in flush_thinking():
                        yield chunk
                    if event.kind == "done":
                        yield _sse("done", _result_payload(event.payload["result"]))
                    elif event.kind == "tool":
                        yield _sse("tool", event.payload)
                    else:
                        yield _sse(event.kind, {"text": event.text})
                async for chunk in flush_thinking():
                    yield chunk
            except LLMError as exc:
                yield _sse("error", {"message": str(exc)})
            except Exception as exc:  # 兜底：别让 SSE 连接静默断掉
                yield _sse("error", {"message": f"内部错误：{exc}"})

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    @router.get("/api/agent/tools")
    async def agent_tools() -> dict[str, Any]:
        return {
            "enabled": agent.settings.tools_enabled,
            "tools": agent.tools.specs(),
            "recent": list(agent.tools.history),
        }

    @router.get("/api/plugins")
    async def plugins_status(request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = dict(agent.plugin_status())
        mcp = getattr(request.app.state, "mcp", None)
        payload["mcp"] = mcp.status() if mcp is not None else {"enabled": False, "servers": []}
        return payload

    @router.get("/project", response_class=HTMLResponse)
    async def project_page() -> str:
        return _render_doc(agent.settings.docs_dir, "PROJECT_MAP.md", "项目结构")

    @router.get("/plugins-guide", response_class=HTMLResponse)
    async def plugins_guide_page() -> str:
        return _render_doc(agent.settings.docs_dir, "PLUGINS.md", "插件指南")

    @router.delete("/api/session/{session_id}")
    async def reset_session(session_id: str) -> dict[str, bool]:
        await agent.reset(session_id)
        return {"ok": True}

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        # 名字从人设读，改 persona.yaml 不用改代码
        return INDEX_HTML.replace("__PERSONA_NAME__", agent.persona.name)

    return router


# 用 __PERSONA_NAME__ 占位，避免和 CSS/JS 里的花括号冲突
def _render_doc(docs_dir: str | Path, filename: str, title: str) -> str:
    path = Path(docs_dir) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"找不到文档：{filename}")
    body = _MD.render(path.read_text(encoding="utf-8"))
    return _DOC_PAGE.replace("{{TITLE}}", title).replace("{{BODY}}", body)


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PERSONA_NAME__ · 调试页</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f6f4f1; color: #2a2622; display: flex; flex-direction: column; height: 100vh; }
  header { padding: 14px 18px; border-bottom: 1px solid #e2ddd6; font-weight: 600; }
  header small { font-weight: 400; opacity: .55; margin-left: 8px; }
  #out { flex: 1; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .row { display: flex; }
  .row.me { justify-content: flex-end; }
  .bubble { max-width: 76%; padding: 9px 13px; border-radius: 14px; white-space: pre-wrap; word-break: break-word; }
  .me .bubble { background: #2a2622; color: #f6f4f1; border-bottom-right-radius: 4px; }
  .her .bubble { background: #fff; border: 1px solid #e8e3dc; border-bottom-left-radius: 4px; }
  .meta { font-size: 12px; opacity: .55; margin-top: 4px; }
  .meta a { color: inherit; }
  form { display: flex; gap: 8px; padding: 14px 18px; border-top: 1px solid #e2ddd6; background: #fbfaf8; }
  textarea { flex: 1; resize: none; height: 46px; padding: 10px 12px; border-radius: 10px;
             border: 1px solid #ddd7cf; font: inherit; background: #fff; }
  button { padding: 0 20px; border: 0; border-radius: 10px; background: #2a2622; color: #fff; font: inherit; cursor: pointer; }
  button:disabled { opacity: .4; cursor: default; }
  .tool { font-size: 12px; opacity: .6; }
</style>
</head>
<body>
<header>
  __PERSONA_NAME__ <small id="status">连接中…</small>
  <span style="float:right;font-weight:400;font-size:13px">
    <a href="/project" style="color:inherit">项目结构</a> ·
    <a href="/plugins-guide" style="color:inherit">插件指南</a> ·
    <a href="/api/plugins" style="color:inherit">插件状态</a>
  </span>
</header>
<div id="out"></div>
<form id="form">
  <textarea id="input" placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"></textarea>
  <button id="send" type="submit">发送</button>
</form>
<script>
const out = document.getElementById('out');
const form = document.getElementById('form');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
const sessionId = 'web-' + Math.random().toString(36).slice(2, 8);

fetch('/healthz').then(r => r.json()).then(d => {
  statusEl.textContent = `${d.persona} · ${d.model} · ${d.llm} · 联网${d.web_tools ? '开' : '关'}`;
}).catch(() => { statusEl.textContent = '离线'; });

function addRow(kind, text) {
  const row = document.createElement('div');
  row.className = 'row ' + kind;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  row.appendChild(bubble);
  out.appendChild(row);
  out.scrollTop = out.scrollHeight;
  return row;
}

function renderMeta(row, citations) {
  if (!citations || !citations.length) return;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.innerHTML = '来源：' + citations.map(c =>
    `<a href="${c.url}" target="_blank" rel="noreferrer">${c.title || c.url}</a>`).join(' · ');
  row.querySelector('.bubble').appendChild(meta);
}

async function send(text) {
  addRow('me', text);
  const row = addRow('her', '');
  const bubble = row.querySelector('.bubble');
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, session_id: sessionId }),
  });
  if (!res.ok || !res.body) { bubble.textContent = '（请求失败）'; return; }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\\n\\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      let eventName = 'message', data = '';
      for (const line of part.split('\\n')) {
        if (line.startsWith('event: ')) eventName = line.slice(7);
        else if (line.startsWith('data: ')) data = line.slice(6);
      }
      if (!data) continue;
      let payload;
      try { payload = JSON.parse(data); } catch (e) { continue; }
      if (eventName === 'text') bubble.textContent += payload.text;
      else if (eventName === 'thinking') { /* 需要的话可以显示思考中 */ }
      else if (eventName === 'tool') {
        const tip = document.createElement('div');
        tip.className = 'tool';
        if (payload.type === 'function_call') tip.textContent = '🛠 ' + payload.name + '…';
        else if (payload.type === 'function_result' && !payload.ok) tip.textContent = '⚠️ ' + (payload.preview || '没成功');
        else tip.textContent = '🔍 正在上网查…';
        bubble.appendChild(tip);
      }
      else if (eventName === 'done') { bubble.textContent = payload.text; renderMeta(row, payload.citations); }
      else if (eventName === 'error') bubble.textContent = '（出错了：' + payload.message + '）';
    }
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  sendBtn.disabled = true;
  try { await send(text); } finally { sendBtn.disabled = false; input.focus(); }
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.focus();
</script>
</body>
</html>
"""
