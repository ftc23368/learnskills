"""Async SQLite persistence for conversations and messages.

Why content is stored as JSON, not text:
- Anthropic responses contain a list of content blocks (text, thinking,
  tool_use, tool_result). The next turn must echo these back verbatim —
  including thinking blocks with their `signature` fields, untouched.
- Storing raw text would lose the thinking and tool blocks, breaking
  multi-turn agentic loops.

The `seq` column ensures stable message ordering. We use a composite
primary key on (conversation_id, seq) — much simpler than UUIDs for a
single-user local app, and joins are trivial.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from .config import settings


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    stop_reason     TEXT,
    usage_json      TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, seq),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);
"""


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row
        yield db


async def init_db() -> None:
    async with _connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def create_conversation(title: str = "New chat") -> dict[str, Any]:
    cid = _new_id()
    now = _now()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
        await db.commit()
    return {"id": cid, "title": title, "created_at": now, "updated_at": now}


async def list_conversations() -> list[dict[str, Any]]:
    async with _connect() as db:
        async with db.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_conversation(conversation_id: str) -> bool:
    async with _connect() as db:
        cur = await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        await db.commit()
        return cur.rowcount > 0


async def update_title(conversation_id: str, title: str) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), conversation_id),
        )
        await db.commit()


async def append_message(
    conversation_id: str,
    role: str,
    content: list[dict[str, Any]] | str,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> int:
    """Append a message and return its `seq`.

    `content` is normalized to the Anthropic block-list shape:
    a string becomes [{"type": "text", "text": ...}].
    """
    if isinstance(content, str):
        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    else:
        content_blocks = content

    content_json = json.dumps(content_blocks)
    usage_json = json.dumps(usage) if usage is not None else None
    now = _now()

    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
            seq = int(row["next_seq"])

        await db.execute(
            """INSERT INTO messages
               (conversation_id, seq, role, content_json, stop_reason, usage_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, seq, role, content_json, stop_reason, usage_json, now),
        )
        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        await db.commit()

    return seq


async def get_messages(conversation_id: str) -> list[dict[str, Any]]:
    """Return messages in Anthropic shape, ordered by seq.

    Each item: {role, content (list of blocks), stop_reason, usage, seq, created_at}.
    """
    async with _connect() as db:
        async with db.execute(
            """SELECT seq, role, content_json, stop_reason, usage_json, created_at
               FROM messages WHERE conversation_id = ? ORDER BY seq ASC""",
            (conversation_id,),
        ) as cur:
            rows = await cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "seq": r["seq"],
                "role": r["role"],
                "content": json.loads(r["content_json"]),
                "stop_reason": r["stop_reason"],
                "usage": json.loads(r["usage_json"]) if r["usage_json"] else None,
                "created_at": r["created_at"],
            }
        )
    return out


async def get_messages_for_api(conversation_id: str) -> list[dict[str, Any]]:
    """Return messages in the exact shape the Anthropic SDK expects:
    [{role, content}, ...] — no metadata fields.
    """
    msgs = await get_messages(conversation_id)
    return [{"role": m["role"], "content": m["content"]} for m in msgs]
