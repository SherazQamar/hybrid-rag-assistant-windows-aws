"""Configuration profiles: chunking, embedding, retrieval and context settings editable by admins
without touching Python. Profiles are validated, stored in the DB and exportable/importable as JSON."""
from __future__ import annotations

import copy
import json
import time

from . import db

DEFAULT_EMBEDDING = {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dim": 384}

EXAMPLE_PROFILES: list[dict] = [
    {
        "name": "balanced-hybrid",
        "description": "Recursive chunking on headings/paragraphs/sentences, RRF hybrid fusion. Good default.",
        "chunking": {"strategy": "recursive", "unit": "chars", "size": 800, "overlap": 120,
                     "boundaries": ["heading", "paragraph", "sentence"]},
        "embedding": DEFAULT_EMBEDDING,
        "retrieval": {"default_mode": "hybrid", "dense_candidates": 20, "keyword_candidates": 20,
                      "fusion": "rrf", "rrf_k": 60, "dense_weight": 0.5, "keyword_weight": 0.5,
                      "final_k": 5, "dense_min_score": 0.68},
        "context": {"history_messages": 6, "max_context_chars": 6000, "max_memories": 5},
    },
    {
        "name": "precise-small-chunks",
        "description": "Fixed windows of 120 word-tokens with 20 overlap, weighted fusion favouring keywords.",
        "chunking": {"strategy": "fixed", "unit": "tokens", "size": 120, "overlap": 20,
                     "boundaries": ["sentence"]},
        "embedding": DEFAULT_EMBEDDING,
        "retrieval": {"default_mode": "hybrid", "dense_candidates": 30, "keyword_candidates": 30,
                      "fusion": "weighted", "rrf_k": 60, "dense_weight": 0.4, "keyword_weight": 0.6,
                      "final_k": 6, "dense_min_score": 0.68},
        "context": {"history_messages": 8, "max_context_chars": 5000, "max_memories": 5},
    },
    {
        "name": "large-chunks-no-overlap",
        "description": "Large 1500-char paragraph-packed chunks, zero overlap, keyword-only default.",
        "chunking": {"strategy": "recursive", "unit": "chars", "size": 1500, "overlap": 0,
                     "boundaries": ["heading", "paragraph"]},
        "embedding": DEFAULT_EMBEDDING,
        "retrieval": {"default_mode": "keyword", "dense_candidates": 10, "keyword_candidates": 10,
                      "fusion": "rrf", "rrf_k": 60, "dense_weight": 0.5, "keyword_weight": 0.5,
                      "final_k": 3, "dense_min_score": 0.68},
        "context": {"history_messages": 4, "max_context_chars": 8000, "max_memories": 3},
    },
]

MODES = ("dense", "keyword", "hybrid")


def validate(p: dict) -> dict:
    """Returns a normalised copy or raises ValueError with a readable message."""
    p = copy.deepcopy(p)
    name = str(p.get("name", "")).strip()
    if not name or len(name) > 60 or not all(c.isalnum() or c in "-_ ." for c in name):
        raise ValueError("Profile name: 1-60 chars, letters/digits/-_ .")
    c, r, x = p.get("chunking", {}), p.get("retrieval", {}), p.get("context", {})
    e = p.get("embedding") or DEFAULT_EMBEDDING
    if c.get("strategy") not in ("fixed", "recursive"):
        raise ValueError("chunking.strategy must be 'fixed' or 'recursive'")
    if c.get("unit") not in ("chars", "tokens"):
        raise ValueError("chunking.unit must be 'chars' or 'tokens'")
    size, overlap = int(c.get("size", 0)), int(c.get("overlap", -1))
    if not 20 <= size <= 8000:
        raise ValueError("chunking.size must be 20..8000")
    if not 0 <= overlap < size:
        raise ValueError("chunking.overlap must be >= 0 and < size")
    bounds = [b for b in c.get("boundaries", []) if b in ("heading", "paragraph", "sentence")]
    if r.get("default_mode") not in MODES:
        raise ValueError("retrieval.default_mode must be dense, keyword or hybrid")
    if r.get("fusion") not in ("rrf", "weighted"):
        raise ValueError("retrieval.fusion must be 'rrf' or 'weighted'")
    for k in ("dense_candidates", "keyword_candidates", "final_k"):
        if not 1 <= int(r.get(k, 0)) <= 200:
            raise ValueError(f"retrieval.{k} must be 1..200")
    dw, kw = float(r.get("dense_weight", 0.5)), float(r.get("keyword_weight", 0.5))
    if dw < 0 or kw < 0 or dw + kw == 0:
        raise ValueError("fusion weights must be >= 0 and not both zero")
    if e.get("provider") not in ("fastembed", "hash", "openai", "bedrock"):
        raise ValueError("embedding.provider must be fastembed, hash, openai or bedrock")
    return {
        "name": name,
        "description": str(p.get("description", ""))[:300],
        "chunking": {"strategy": c["strategy"], "unit": c["unit"], "size": size, "overlap": overlap,
                     "boundaries": bounds},
        "embedding": {"provider": e["provider"], "model": str(e.get("model", "")), "dim": int(e.get("dim", 384))},
        "retrieval": {"default_mode": r["default_mode"], "dense_candidates": int(r["dense_candidates"]),
                      "keyword_candidates": int(r["keyword_candidates"]), "fusion": r["fusion"],
                      "rrf_k": int(r.get("rrf_k", 60)), "dense_weight": dw, "keyword_weight": kw,
                      "final_k": int(r["final_k"]), "dense_min_score": float(r.get("dense_min_score", 0.68))},
        "context": {"history_messages": int(x.get("history_messages", 6)),
                    "max_context_chars": int(x.get("max_context_chars", 6000)),
                    "max_memories": int(x.get("max_memories", 5))},
    }


def save(p: dict) -> dict:
    p = validate(p)
    db.execute(
        "INSERT INTO profiles(name, json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at",
        (p["name"], json.dumps(p), time.time()),
    )
    return p


def seed_examples(embedding: dict | None = None) -> None:
    if db.one("SELECT name FROM profiles LIMIT 1"):
        return
    for p in EXAMPLE_PROFILES:
        p = copy.deepcopy(p)
        if embedding:
            p["embedding"] = embedding
        save(p)
    db.kv_set("active_profile", EXAMPLE_PROFILES[0]["name"])


def get(name: str) -> dict | None:
    row = db.one("SELECT json FROM profiles WHERE name = ?", (name,))
    return json.loads(row["json"]) if row else None


def list_all() -> list[dict]:
    return [json.loads(r["json"]) for r in db.query("SELECT json FROM profiles ORDER BY name")]


def active() -> dict:
    return get(db.kv_get("active_profile", EXAMPLE_PROFILES[0]["name"])) or list_all()[0]


def activate(name: str) -> None:
    if not get(name):
        raise ValueError("Unknown profile")
    db.kv_set("active_profile", name)


def delete(name: str) -> None:
    if name == db.kv_get("active_profile"):
        raise ValueError("Cannot delete the active profile")
    db.execute("DELETE FROM profiles WHERE name = ?", (name,))


def export_json(name: str) -> str:
    p = get(name)
    if not p:
        raise ValueError("Unknown profile")
    return json.dumps(p, indent=2)


def import_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    return save(data)
