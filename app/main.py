from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from app.config import Settings, get_settings
from app.dependencies import get_agent_service, get_db, get_knowledge_file_service
from app.schemas import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionCreateResponse,
    ConflictRecord,
    ReloadSourcesResponse,
    SourceStatus,
    SourcesStatusResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_db().init_schema()
    yield


app = FastAPI(title="Bambi Knowledge Agent", version="0.1.0", lifespan=lifespan)


def require_admin_token(admin_api_token: str = Header(default="", alias="X-Admin-Token"), settings: Settings = Depends(get_settings)) -> None:
    if admin_api_token != settings.admin_api_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """
<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bambi Knowledge Agent</title>
  <style>
    :root { --panel: #fffaf0; --ink: #182126; --accent: #c34f2f; --muted: #6e6b66; --line: #dcc9a8; }
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: linear-gradient(135deg, #f6f0e7, #ead9c2); color: var(--ink); }
    .wrap { max-width: 920px; margin: 0 auto; padding: 32px 16px 56px; }
    .hero { background: rgba(255,250,240,.88); border: 1px solid var(--line); border-radius: 24px; padding: 24px; box-shadow: 0 16px 40px rgba(0,0,0,.08); }
    h1 { margin: 0 0 8px; font-size: 32px; }
    p { color: var(--muted); }
    .chat { margin-top: 20px; display: grid; gap: 12px; }
    #messages { background: var(--panel); border: 1px solid var(--line); border-radius: 20px; min-height: 360px; padding: 16px; overflow: auto; }
    .msg { margin-bottom: 14px; padding: 12px 14px; border-radius: 16px; white-space: pre-wrap; }
    .user { background: #efe1ca; }
    .assistant { background: #fff; border: 1px solid #ead8b7; }
    .loading { display: flex; align-items: center; gap: 8px; color: var(--muted); }
    .dots { display: inline-flex; gap: 4px; direction: ltr; }
    .dot { width: 6px; height: 6px; border-radius: 999px; background: var(--accent); animation: pulse 1s infinite ease-in-out; opacity: .35; }
    .dot:nth-child(2) { animation-delay: .15s; }
    .dot:nth-child(3) { animation-delay: .3s; }
    @keyframes pulse { 0%, 80%, 100% { transform: translateY(0); opacity: .35; } 40% { transform: translateY(-4px); opacity: 1; } }
    textarea { width: 100%; min-height: 80px; border-radius: 16px; border: 1px solid var(--line); padding: 14px; font: inherit; resize: vertical; box-sizing: border-box; }
    button { background: var(--accent); color: white; border: 0; border-radius: 14px; padding: 12px 18px; font: inherit; cursor: pointer; }
    button:disabled { opacity: .6; cursor: wait; }
    .meta { font-size: 13px; color: var(--muted); }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>סביבת בדיקה מקומית לבוט הידע של במבי</h1>
      <p>הבוט עונה רק מתוך כלי הידע המקומיים בפרויקט. אם אין מידע מספיק, הוא יבקש הבהרה או יסמן צורך בבדיקה אנושית.</p>
      <div class="chat">
        <div id="messages"></div>
        <textarea id="input" placeholder="שאל/י על קורס, מחיר, דרישות, מועדים או מסמך ידע"></textarea>
        <button id="send">שלח</button>
        <div class="meta" id="meta"></div>
      </div>
    </div>
  </div>
  <script>
    let sessionId = null;
    const messages = document.getElementById('messages');
    const input = document.getElementById('input');
    const meta = document.getElementById('meta');
    const sendButton = document.getElementById('send');

    function addMessage(role, content) {
      const el = document.createElement('div');
      el.className = 'msg ' + role;
      el.textContent = content;
      messages.appendChild(el);
      messages.scrollTop = messages.scrollHeight;
      return el;
    }

    function addLoadingMessage() {
      const el = document.createElement('div');
      el.className = 'msg assistant loading';
      el.innerHTML = '<span class="loading-text">מחפש מידע מתאים...</span><span class="dots"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>';
      messages.appendChild(el);
      messages.scrollTop = messages.scrollHeight;
      return el;
    }

    function formatAnswer(payload) {
      let body = payload.answer || '';
      if (payload.follow_up_question) body += "\\n\\nשאלת המשך: " + payload.follow_up_question;
      if (payload.needs_human_review) body += "\\n\\nנדרש בירור אנושי.";
      return body;
    }

    async function ensureSession() {
      if (sessionId) return sessionId;
      const res = await fetch('/chat/sessions', { method: 'POST' });
      const data = await res.json();
      sessionId = data.session_id;
      meta.textContent = 'Session: ' + sessionId;
      return sessionId;
    }

    async function send() {
      const text = input.value.trim();
      if (!text || sendButton.disabled) return;
      addMessage('user', text);
      input.value = '';
      sendButton.disabled = true;
      const loadingEl = addLoadingMessage();

      try {
        const sid = await ensureSession();
        const res = await fetch(`/chat/sessions/${sid}/messages/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text })
        });
        if (!res.ok || !res.body) throw new Error('Request failed');

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let assistantEl = null;

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (!line.trim()) continue;
            const event = JSON.parse(line);
            if (event.type === 'status') {
              loadingEl.querySelector('.loading-text').textContent = event.message;
            } else if (event.type === 'delta') {
              if (!assistantEl) {
                loadingEl.remove();
                assistantEl = addMessage('assistant', '');
              }
              assistantEl.textContent += event.delta;
              messages.scrollTop = messages.scrollHeight;
            } else if (event.type === 'final') {
              if (!assistantEl) {
                loadingEl.remove();
                addMessage('assistant', formatAnswer(event.response));
              }
            }
          }
        }
      } catch (err) {
        loadingEl.remove();
        addMessage('assistant', 'אירעה שגיאה בשליחת ההודעה. נסה שוב בעוד רגע.');
      } finally {
        sendButton.disabled = false;
        input.focus();
      }
    }

    sendButton.addEventListener('click', send);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
  </script>
</body>
</html>
"""


@app.post("/chat/sessions", response_model=ChatSessionCreateResponse)
async def create_session():
    session_id, created_at = get_agent_service().create_session()
    return ChatSessionCreateResponse(session_id=session_id, created_at=created_at)


@app.post("/chat/sessions/{session_id}/messages", response_model=ChatMessageResponse)
async def send_message(session_id: str, payload: ChatMessageRequest):
    response = await get_agent_service().ask(session_id, payload.message)
    return ChatMessageResponse(session_id=session_id, response=response, created_at=datetime.now(UTC))


@app.post("/chat/sessions/{session_id}/messages/stream")
async def stream_message(session_id: str, payload: ChatMessageRequest):
    async def events():
        async for event in get_agent_service().ask_stream(session_id, payload.message):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/chat/sessions/{session_id}")
async def get_session(session_id: str):
    session = get_agent_service().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.post("/admin/reload-sources", response_model=ReloadSourcesResponse, dependencies=[Depends(require_admin_token)])
async def reload_sources():
    return ReloadSourcesResponse(run_id=0, status="disabled", message="Online ingestion is disabled. The agent uses local files in app/tools-knowleage.")


@app.get("/admin/sources/status", response_model=SourcesStatusResponse, dependencies=[Depends(require_admin_token)])
async def source_status():
    tools = get_knowledge_file_service().list_tools()
    statuses = [
        SourceStatus(
            source_type="local_knowledge_files",
            total_sources=sum(1 for item in tools if item["file_name"]),
            total_chunks=0,
            latest_success_at=None,
            latest_error=None,
        )
    ]
    return SourcesStatusResponse(statuses=statuses)


@app.get("/admin/conflicts", response_model=list[ConflictRecord], dependencies=[Depends(require_admin_token)])
async def conflicts():
    rows = get_db().get_conflicts()
    return [
        ConflictRecord(
            id=row["id"],
            key=row["key"],
            field_name=row["field_name"],
            primary_value=row["primary_value"],
            secondary_value=row["secondary_value"],
            status=row["status"],
            source_ids=[row["primary_source_id"], row["secondary_source_id"]],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]
