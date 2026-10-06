"""Admin helpers: chunk previews for trying settings before reindexing, and usage summaries."""
from __future__ import annotations

import time

from . import db, ingestion, profiles
from .config import Settings


def chunk_preview(doc_id: int, profile: dict, settings: Settings, limit: int = 50) -> dict:
    """Chunks a document with ANY profile (unsaved is fine) without touching the live index."""
    p = profiles.validate(profile)
    doc = db.one("SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,))
    if not doc:
        raise LookupError("Document not found")
    segments = ingestion.extract(doc["file_type"], (settings.upload_dir / doc["stored_name"]).read_bytes())
    chunks = ingestion.chunk_segments(segments, p["chunking"])
    return {
        "document": doc["filename"], "settings": p["chunking"], "total_chunks": len(chunks),
        "chunks": [{"ordinal": i, "page": c.page, "section": c.section, "start_char": c.start_char,
                    "end_char": c.end_char, "chars": c.n_chars, "tokens": c.n_tokens, "text": c.text}
                   for i, c in enumerate(chunks[:limit])],
    }


def usage_summary(hours: int = 24) -> dict:
    since = time.time() - hours * 3600
    rows = db.query("SELECT kind, COUNT(*) AS n, SUM(1 - ok) AS errors, AVG(latency_ms) AS avg_ms "
                    "FROM usage_log WHERE created_at > ? GROUP BY kind ORDER BY n DESC", (since,))
    lat = [r["latency_ms"] for r in db.query("SELECT latency_ms FROM usage_log WHERE kind='chat.ask' AND "
                                              "created_at > ? AND latency_ms IS NOT NULL ORDER BY latency_ms", (since,))]
    p = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else None  # noqa: E731
    return {"by_kind": rows, "chat_p50_ms": p(0.5), "chat_p95_ms": p(0.95), "questions": len(lat)}
