"""Document lifecycle and versioned indexing.

* Each index version records its chunking settings + embedding model/dimension.
* Queries always use the ACTIVE version's embedding model -> no incompatible vectors.
* Reindex builds a NEW version in the background; it only becomes active when every document
  has been processed. A failed/incomplete build is discarded and the old version keeps serving.
* Deleting a document removes its chunks (dense + keyword entries) from every version at once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid

from . import db, embeddings, ingestion, profiles
from .config import Settings
from .security import PermissionDenied, QuotaExceeded, User

log = logging.getLogger("indexing")
_reindex_lock = threading.Lock()


# ---------------- index versions ----------------

def active_version() -> dict | None:
    return db.one("SELECT * FROM index_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1")


def building_version() -> dict | None:
    return db.one("SELECT * FROM index_versions WHERE status = 'building' ORDER BY id DESC LIMIT 1")


def list_versions() -> list[dict]:
    return db.query("SELECT v.*, (SELECT COUNT(*) FROM chunks c WHERE c.index_version = v.id) AS n_chunks "
                    "FROM index_versions v ORDER BY id DESC")


def _new_version(profile: dict, status: str) -> int:
    e = profile["embedding"]
    return db.execute(
        "INSERT INTO index_versions(profile_name, chunking_json, embed_provider, embed_model, embed_dim, status, "
        "progress, created_at, activated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (profile["name"], json.dumps(profile["chunking"]), e["provider"], e["model"], e["dim"], status,
         1.0 if status == "active" else 0.0, time.time(), time.time() if status == "active" else None),
    )


def ensure_active_version() -> None:
    # A build interrupted by a crash/restart can never become active.
    db.execute("UPDATE index_versions SET status='failed', error='interrupted by restart' WHERE status='building'")
    db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT c.id FROM chunks c JOIN index_versions v "
               "ON v.id = c.index_version WHERE v.status = 'failed')")
    db.execute("DELETE FROM chunks WHERE index_version IN (SELECT id FROM index_versions WHERE status = 'failed')")
    db.execute("UPDATE documents SET status='queued' WHERE status='processing'")
    if not active_version():
        _new_version(profiles.active(), "active")


def version_embed_cfg(v: dict) -> dict:
    return {"provider": v["embed_provider"], "model": v["embed_model"], "dim": v["embed_dim"]}


def reindex_needed() -> bool:
    v, p = active_version(), profiles.active()
    return bool(v) and (json.loads(v["chunking_json"]) != p["chunking"] or version_embed_cfg(v) != p["embedding"])


# ---------------- documents ----------------

def visible_documents(user: User) -> list[dict]:
    cols = ("d.id, d.filename, d.file_type, d.visibility, d.status, d.error, d.size_bytes, d.n_chunks, "
            "d.created_at, d.owner_id, u.username AS owner")
    if user.is_admin:
        return db.query(f"SELECT {cols} FROM documents d JOIN users u ON u.id = d.owner_id "
                        "WHERE d.deleted_at IS NULL ORDER BY d.id DESC")
    return db.query(f"SELECT {cols} FROM documents d JOIN users u ON u.id = d.owner_id WHERE d.deleted_at IS NULL "
                    "AND (d.owner_id = ? OR d.visibility = 'shared') ORDER BY d.id DESC", (user.id,))


def upload(user: User, filename: str, data: bytes, visibility: str, settings: Settings) -> int:
    if visibility not in ("private", "shared"):
        raise ValueError("visibility must be private or shared")
    if visibility == "shared" and not user.is_admin:
        raise PermissionDenied("Only admins can publish shared documents")
    n = db.one("SELECT COUNT(*) AS n FROM documents WHERE owner_id = ? AND deleted_at IS NULL", (user.id,))["n"]
    if n >= settings.max_docs_per_user:
        raise QuotaExceeded("Document quota reached")
    name = ingestion.safe_filename(filename)
    ftype = ingestion.validate_upload(name, data, settings.max_upload_mb * 1024 * 1024)
    sha = hashlib.sha256(data).hexdigest()
    if db.one("SELECT id FROM documents WHERE owner_id = ? AND sha256 = ? AND deleted_at IS NULL", (user.id, sha)):
        raise ValueError("This file is already uploaded")
    stored = f"{uuid.uuid4().hex}.bin"  # never use the client filename on disk
    (settings.upload_dir / stored).write_bytes(data)
    doc_id = db.execute(
        "INSERT INTO documents(owner_id, filename, file_type, visibility, status, sha256, stored_name, size_bytes, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (user.id, name, ftype, visibility, "queued", sha, stored, len(data), time.time()),
    )
    db.log_usage(user.id, "doc.upload", {"doc_id": doc_id, "type": ftype, "bytes": len(data)})
    if settings.ingest_sync:
        process_document(doc_id, settings)
    else:
        threading.Thread(target=process_document, args=(doc_id, settings), daemon=True).start()
    return doc_id


def _insert_chunks(con, doc_id: int, version_id: int, chunks: list[ingestion.Chunk], vecs) -> None:
    for i, (c, v) in enumerate(zip(chunks, vecs)):
        cur = con.execute(
            "INSERT INTO chunks(doc_id, index_version, ordinal, text, page, section, start_char, end_char, n_chars, "
            "n_tokens, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, version_id, i, c.text, c.page, c.section, c.start_char, c.end_char, c.n_chars, c.n_tokens,
             v.astype("float32").tobytes()),
        )
        con.execute("INSERT INTO chunks_fts(rowid, text, chunk_id) VALUES (?,?,?)", (cur.lastrowid, c.text, cur.lastrowid))


def _index_doc_into(doc: dict, version: dict, settings: Settings) -> int:
    data = (settings.upload_dir / doc["stored_name"]).read_bytes()
    segments = ingestion.extract(doc["file_type"], data)
    chunks = ingestion.chunk_segments(segments, json.loads(version["chunking_json"]))
    if not chunks:
        raise ingestion.IngestError("No text chunks produced")
    vecs = embeddings.embed([c.text for c in chunks], version_embed_cfg(version), settings)
    with db.connect() as con:  # one transaction: replace this doc's chunks in this version
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=? AND index_version=?)",
                    (doc["id"], version["id"]))
        con.execute("DELETE FROM chunks WHERE doc_id = ? AND index_version = ?", (doc["id"], version["id"]))
        _insert_chunks(con, doc["id"], version["id"], chunks, vecs)
    return len(chunks)


def process_document(doc_id: int, settings: Settings) -> None:
    db.execute("UPDATE documents SET status='processing', error=NULL WHERE id=? AND deleted_at IS NULL", (doc_id,))
    doc = db.one("SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,))
    if not doc:
        return
    t0 = time.perf_counter()
    try:
        n = _index_doc_into(doc, active_version(), settings)
        if building := building_version():  # keep an in-progress rebuild complete
            _index_doc_into(doc, building, settings)
        db.execute("UPDATE documents SET status='ready', n_chunks=? WHERE id=? AND deleted_at IS NULL", (n, doc_id))
        db.log_usage(doc["owner_id"], "doc.ingest", {"doc_id": doc_id, "chunks": n}, (time.perf_counter() - t0) * 1000)
    except Exception as exc:  # status + error visible to user and admin
        log.exception("ingestion failed for %s", doc_id)
        db.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(exc)[:500], doc_id))
        db.log_usage(doc["owner_id"], "doc.ingest", {"doc_id": doc_id, "error": str(exc)[:500]}, ok=False)


def delete_document(user: User, doc_id: int, settings: Settings) -> None:
    doc = db.one("SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,))
    if not doc or (doc["owner_id"] != user.id and not user.is_admin):
        raise LookupError("Document not found")  # same answer whether it exists or not
    with db.connect() as con:
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
        con.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        con.execute("UPDATE documents SET deleted_at = ?, status = 'deleted' WHERE id = ?", (time.time(), doc_id))
    if settings.delete_originals and doc["stored_name"]:
        (settings.upload_dir / doc["stored_name"]).unlink(missing_ok=True)
    db.log_usage(user.id, "doc.delete", {"doc_id": doc_id})


# ---------------- reindex ----------------

def start_reindex(settings: Settings, fail_after: int | None = None) -> int:
    """Builds a new version from the active profile. `fail_after` simulates a crash (tests/demo)."""
    if not _reindex_lock.acquire(blocking=False):
        raise ValueError("A reindex is already running")
    try:
        version_id = _new_version(profiles.active(), "building")
    except Exception:
        _reindex_lock.release()
        raise
    if settings.ingest_sync:
        _run_reindex(version_id, settings, fail_after)
    else:
        threading.Thread(target=_run_reindex, args=(version_id, settings, fail_after), daemon=True).start()
    return version_id


def _run_reindex(version_id: int, settings: Settings, fail_after: int | None) -> None:
    try:
        version = db.one("SELECT * FROM index_versions WHERE id = ?", (version_id,))
        done: set[int] = set()
        while True:  # loop again to catch documents uploaded while the build was running
            docs = [d for d in db.query("SELECT * FROM documents WHERE status IN ('ready','processing','queued') "
                                        "AND deleted_at IS NULL ORDER BY id") if d["id"] not in done]
            if not docs:
                break
            for i, d in enumerate(docs):
                if fail_after is not None and len(done) >= fail_after:
                    raise RuntimeError("Simulated failure during reindex")
                n = _index_doc_into(d, version, settings)
                db.execute("UPDATE documents SET n_chunks = ? WHERE id = ?", (n, d["id"]))
                done.add(d["id"])
                total = len(done) + len(docs) - i - 1
                db.execute("UPDATE index_versions SET progress = ? WHERE id = ?", (len(done) / max(total, 1), version_id))
        with db.connect() as con:  # atomic switch-over; old version's chunks removed -> no stale/duplicates
            old = con.execute("SELECT id FROM index_versions WHERE status = 'active'").fetchall()
            for (old_id,) in old:
                con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE index_version = ?)", (old_id,))
                con.execute("DELETE FROM chunks WHERE index_version = ?", (old_id,))
                con.execute("UPDATE index_versions SET status = 'retired' WHERE id = ?", (old_id,))
            con.execute("UPDATE index_versions SET status='active', progress=1, activated_at=? WHERE id=?",
                        (time.time(), version_id))
        db.log_usage(None, "index.activate", {"version": version_id, "docs": len(done)})
    except Exception as exc:
        log.exception("reindex failed")
        with db.connect() as con:
            con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE index_version = ?)", (version_id,))
            con.execute("DELETE FROM chunks WHERE index_version = ?", (version_id,))
            con.execute("UPDATE index_versions SET status='failed', error=? WHERE id=?", (str(exc)[:500], version_id))
        db.log_usage(None, "index.failed", {"version": version_id, "error": str(exc)[:500]}, ok=False)
    finally:
        _reindex_lock.release()
