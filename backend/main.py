"""FastAPI app: REST endpoints + SSE streaming endpoint.

Conventions:
- All Anthropic calls happen server-side. The frontend never sees the API key.
- Bind to 127.0.0.1 by default (local-only).
- CORS allowlist is the localhost origins only.
- Per-conversation asyncio.Lock prevents concurrent streams from racing on
  message ordering in SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import agent, db
from .config import settings
from .skills import load_skills

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.log_level)


# Per-conversation lock to serialize streams; rejects concurrent stream
# attempts to the same conversation with 409. Locks are created lazily and
# never garbage-collected — fine for a single-user local app.
_conversation_locks: dict[str, asyncio.Lock] = {}


def _lock_for(conversation_id: str) -> asyncio.Lock:
    lock = _conversation_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[conversation_id] = lock
    return lock


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    app.state.skills = load_skills(settings.skills_dir)
    logger.info("LearnSkills server ready (%d skills loaded)", len(app.state.skills))
    yield


app = FastAPI(title="LearnSkills", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{settings.port}",
        f"http://localhost:{settings.port}",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------ Schemas ------------------


class CreateConversationResponse(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float


class SendMessageRequest(BaseModel):
    content: str


# ------------------ Endpoints ------------------


@app.post("/api/conversations", response_model=CreateConversationResponse)
async def create_conversation():
    conv = await db.create_conversation()
    return conv


@app.get("/api/conversations")
async def list_conversations():
    return await db.list_conversations()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conv = await db.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await db.get_messages(conversation_id)
    return {**conv, "messages": messages}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    ok = await db.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _conversation_locks.pop(conversation_id, None)
    return {"deleted": True}


@app.get("/api/skills")
async def list_skills(request: Request):
    skills = request.app.state.skills
    return [{"name": s.name, "description": s.description} for s in skills.values()]


@app.post("/api/skills/reload")
async def reload_skills(request: Request):
    request.app.state.skills = load_skills(settings.skills_dir)
    return {"count": len(request.app.state.skills)}


def _shorten_title(text: str, limit: int = 60) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text or "New chat"
    return text[: limit - 1].rstrip() + "…"


@app.post("/api/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, body: SendMessageRequest, request: Request):
    """Stream a turn for `conversation_id`.

    Persists the user message, then opens an SSE stream of typed agent events.
    On `turn_end`, persists the assistant message (with thinking + tool blocks).
    On client disconnect, persists whatever accumulated as `interrupted`.
    """
    conv = await db.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    lock = _lock_for(conversation_id)
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another stream is already active for this conversation.",
        )

    skills = request.app.state.skills

    # Persist user message before streaming so it survives disconnects.
    await db.append_message(conversation_id, "user", body.content)

    # First user message also seeds the conversation title.
    if conv["title"] == "New chat":
        await db.update_title(conversation_id, _shorten_title(body.content))

    history = await db.get_messages_for_api(conversation_id)

    async def event_stream() -> AsyncIterator[dict]:
        async with lock:
            final_content: list[dict[str, Any]] = []
            final_usage: dict[str, Any] | None = None
            final_reason: str | None = None
            errored = False

            try:
                async for ev in agent.run_turn(history, skills):
                    # Capture for persistence
                    if ev["type"] == "turn_end":
                        final_content = ev.get("final_content") or []
                        final_usage = ev.get("usage")
                        final_reason = ev.get("reason")
                    elif ev["type"] == "error":
                        errored = True

                    yield {"event": ev["type"], "data": json.dumps(ev)}

                    # Detect client disconnect early — sse-starlette will
                    # raise but we also poll for safety.
                    if await request.is_disconnected():
                        final_reason = "interrupted"
                        break
            except asyncio.CancelledError:
                final_reason = "interrupted"
                raise
            finally:
                # Persist whatever we accumulated. Even partial / interrupted
                # turns get saved so the user can see what happened.
                if final_content:
                    await db.append_message(
                        conversation_id,
                        "assistant",
                        final_content,
                        stop_reason=final_reason,
                        usage=final_usage,
                    )
                # Closing event so the client knows to clean up.
                yield {"event": "done", "data": json.dumps({"reason": final_reason})}

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return EventSourceResponse(event_stream(), headers=headers, ping=15)


# ------------------ Static frontend ------------------

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def root():
    index = FRONTEND_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="frontend/index.html not found")
    return FileResponse(index)
