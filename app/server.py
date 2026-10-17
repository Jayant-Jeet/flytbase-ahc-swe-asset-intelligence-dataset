"""FastAPI app: chat (SSE), users, conversations, demo controls, and the static frontend."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, memory
from .agent import run_turn
from .llm import get_provider, llm_configured

app = FastAPI(title="Asset Intelligence Copilot")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _conn():
    return db.connect(config.db_path())


class ChatIn(BaseModel):
    user_id: str
    message: str
    conversation_id: str | None = None


@app.get("/api/status")
def status():
    c = _conn()
    try:
        n = c.execute("SELECT COUNT(*) FROM wells").fetchone()[0]
        return {"llm_configured": llm_configured(), "wells": n, "today": memory.logical_today(c).isoformat()}
    finally:
        c.close()


@app.get("/api/users")
def users():
    c = _conn()
    try:
        return [dict(r) for r in c.execute("SELECT user_id, name, role FROM users ORDER BY user_id")]
    finally:
        c.close()


@app.get("/api/conversations")
def conversations(user_id: str):
    c = _conn()
    try:
        return [dict(r) for r in c.execute(
            "SELECT conv_id, day, title FROM conversations WHERE user_id=? ORDER BY conv_id DESC", (user_id,))]
    finally:
        c.close()


@app.get("/api/conversations/{conv_id}/messages")
def conversation_messages(conv_id: str, user_id: str):
    c = _conn()
    try:
        own = c.execute("SELECT 1 FROM conversations WHERE conv_id=? AND user_id=?", (conv_id, user_id)).fetchone()
        if not own:
            raise HTTPException(404, "conversation not found")
        return [dict(r) for r in c.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND user_id=? AND role IN ('user','assistant') ORDER BY msg_id",
            (conv_id, user_id))]
    finally:
        c.close()


@app.post("/api/chat")
def chat(body: ChatIn):
    if not llm_configured():
        raise HTTPException(503, "No LLM configured. Set GOOGLE_API_KEY (or LLM_PROVIDER/OPENROUTER_API_KEY) in .env.")

    def gen():
        c = _conn()
        try:
            for ev in run_turn(c, get_provider(), body.user_id, body.message, body.conversation_id):
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            c.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/next-day")
def next_day():
    c = _conn()
    try:
        memory.advance_day(c, 1)
        return {"today": memory.logical_today(c).isoformat()}
    finally:
        c.close()


@app.post("/api/reset-memory")
def reset_memory():
    c = _conn()
    try:
        memory.reset(c)
        return {"ok": True}
    finally:
        c.close()


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.server:app", host="0.0.0.0", port=8000)
