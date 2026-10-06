"""Long-term, user-specific memory (separate from chat history and from document RAG).

* History  = the raw messages of one chat (within-chat context, last N messages sent to the model).
* Memory   = short approved facts about the user, recalled in EVERY new chat, persisted in the DB so
             they survive logout, app restart and Windows reboot.
* RAG      = passages retrieved from documents for each question.
"""
from __future__ import annotations

import time

from . import db, retrieval
from .security import User

MAX_MEMORY_CHARS = 500


def list_for(user: User) -> list[dict]:
    return db.query("SELECT id, content, status, origin, created_at FROM memories WHERE user_id = ? "
                    "ORDER BY status DESC, id DESC", (user.id,))


def add(user: User, content: str, status: str, origin: str) -> int:
    content = " ".join((content or "").split())[:MAX_MEMORY_CHARS]
    if not content:
        raise ValueError("Memory text is empty")
    return db.execute("INSERT INTO memories(user_id, content, status, origin, created_at) VALUES (?,?,?,?,?)",
                      (user.id, content, status, origin, time.time()))


def approve(user: User, memory_id: int) -> None:
    if not db.one("SELECT id FROM memories WHERE id = ? AND user_id = ?", (memory_id, user.id)):
        raise LookupError("Memory not found")
    db.execute("UPDATE memories SET status = 'active' WHERE id = ? AND user_id = ?", (memory_id, user.id))


def delete(user: User, memory_id: int) -> None:
    if not db.one("SELECT id FROM memories WHERE id = ? AND user_id = ?", (memory_id, user.id)):
        raise LookupError("Memory not found")
    db.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user.id))


def clear(user: User) -> None:
    db.execute("DELETE FROM memories WHERE user_id = ?", (user.id,))


def recall(user: User, query: str, limit: int) -> list[dict]:
    """Active memories ranked by term overlap with the question (all of them if few)."""
    rows = db.query("SELECT id, content FROM memories WHERE user_id = ? AND status = 'active' ORDER BY id DESC",
                    (user.id,))
    terms = retrieval.query_terms(query)
    rows.sort(key=lambda m: -sum(1 for t in terms if t[:5] in m["content"].lower()))
    return rows[:limit]
