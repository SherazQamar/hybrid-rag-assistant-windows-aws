"""Dense, keyword and hybrid retrieval with full score transparency.

* Dense   : cosine similarity (dot product of L2-normalised vectors), exact search with NumPy.
* Keyword : SQLite FTS5 full-text index with its built-in BM25 ranking function (`bm25()`, k1=1.2, b=0.75),
            Porter stemming, stop-words removed, query terms OR-ed. (PostgreSQL version uses ts_rank_cd,
            which is NOT BM25 - documented as such.)
* Hybrid  : weighted Reciprocal Rank Fusion  score = w_d/(k+rank_d) + w_k/(k+rank_k)
            or weighted min-max score fusion  score = (w_d*norm_d + w_k*norm_k)/(w_d+w_k).
Permissions are applied INSIDE the retrieval SQL: only the caller's private docs + shared docs.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import numpy as np

from . import db, embeddings, indexing
from .security import User

STOPWORDS = set("""a an and are as at be by can could did do does for from had has have how i if in into is it its
me my of on or our please should so tell than that the their them then there these they this to was we were what
when where which who whom why will with would you your about any some all also just""".split())


@dataclass
class Retrieved:
    chunk_id: int
    doc_id: int
    filename: str
    page: int | None
    section: str | None
    ordinal: int
    text: str
    dense_score: float | None = None
    dense_rank: int | None = None
    keyword_score: float | None = None
    keyword_rank: int | None = None
    fused_score: float = 0.0
    final_rank: int = 0
    term_coverage: float = 0.0

    def ref(self) -> str:
        loc = f"p.{self.page}" if self.page else (f"§ {self.section}" if self.section else "")
        return f"{self.filename}{' ' + loc if loc else ''} (chunk {self.ordinal})"


@dataclass
class RetrievalResult:
    results: list[Retrieved]
    supported: bool
    debug: dict = field(default_factory=dict)


def query_terms(q: str) -> list[str]:
    return [t for t in dict.fromkeys(re.findall(r"\w+", q.lower())) if t not in STOPWORDS and len(t) > 1]


def _fts_query(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)  # every term quoted -> no FTS syntax injection


_PERM_SQL = ("c.index_version = ? AND d.deleted_at IS NULL AND d.status = 'ready' "
             "AND (d.owner_id = ? OR d.visibility = 'shared')")


def _meta(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT c.id, c.doc_id, c.ordinal, c.text, c.page, c.section, d.filename FROM chunks c "
                    f"JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})", ids)
    return {r["id"]: r for r in rows}


def dense_search(query: str, user: User, version: dict, n: int, settings) -> list[tuple[int, float]]:
    rows = db.query(f"SELECT c.id, c.embedding FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE {_PERM_SQL}",
                    (version["id"], user.id))
    if not rows:
        return []
    qv = embeddings.embed([query], indexing.version_embed_cfg(version), settings)[0]
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    sims = mat @ qv
    top = np.argsort(-sims)[:n]
    return [(rows[i]["id"], float(sims[i])) for i in top]


def keyword_search(query: str, user: User, version: dict, n: int) -> list[tuple[int, float]]:
    terms = query_terms(query)
    if not terms:
        return []
    rows = db.query(
        f"SELECT c.id, bm25(chunks_fts) AS s FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        f"JOIN documents d ON d.id = c.doc_id WHERE chunks_fts MATCH ? AND {_PERM_SQL} ORDER BY s LIMIT ?",
        (_fts_query(terms), version["id"], user.id, n),
    )
    return [(r["id"], -float(r["s"])) for r in rows]  # FTS5 bm25() is negative: lower = better


def _minmax(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    return {k: (1.0 if hi == lo else (v - lo) / (hi - lo)) for k, v in scores.items()}


def retrieve(query: str, user: User, mode: str, rcfg: dict, settings, log: bool = True) -> RetrievalResult:
    t0 = time.perf_counter()
    version = indexing.active_version()
    if not version:
        return RetrievalResult([], False, {"error": "no active index"})
    dense = dense_search(query, user, version, rcfg["dense_candidates"], settings) if mode in ("dense", "hybrid") else []
    keyword = keyword_search(query, user, version, rcfg["keyword_candidates"]) if mode in ("keyword", "hybrid") else []
    d_rank = {cid: i + 1 for i, (cid, _) in enumerate(dense)}
    k_rank = {cid: i + 1 for i, (cid, _) in enumerate(keyword)}
    d_score, k_score = dict(dense), dict(keyword)
    ids = list(dict.fromkeys([c for c, _ in dense] + [c for c, _ in keyword]))
    wd, wk = rcfg["dense_weight"], rcfg["keyword_weight"]
    if mode == "dense":
        fused = d_score
    elif mode == "keyword":
        fused = k_score
    elif rcfg["fusion"] == "rrf":
        k = rcfg["rrf_k"]
        fused = {c: (wd / (k + d_rank[c]) if c in d_rank else 0) + (wk / (k + k_rank[c]) if c in k_rank else 0) for c in ids}
    else:
        nd, nk = _minmax(d_score), _minmax(k_score)
        fused = {c: (wd * nd.get(c, 0) + wk * nk.get(c, 0)) / (wd + wk) for c in ids}
    ordered = sorted(ids, key=lambda c: -fused[c])[: rcfg["final_k"]]
    meta = _meta(ordered)
    terms = query_terms(query)
    results = []
    for rank, cid in enumerate(ordered, start=1):
        m = meta[cid]
        low = m["text"].lower()
        cov = sum(1 for t in terms if t[:5] in low) / len(terms) if terms else 0.0
        results.append(Retrieved(cid, m["doc_id"], m["filename"], m["page"], m["section"], m["ordinal"], m["text"],
                                 d_score.get(cid), d_rank.get(cid), k_score.get(cid), k_rank.get(cid),
                                 float(fused[cid]), rank, cov))
    # "Supported" gate for unanswerable questions: strong semantic match OR most query terms present.
    supported = any((r.dense_score or 0) >= rcfg["dense_min_score"] or r.term_coverage >= 0.6 for r in results)
    debug = {
        "query": query, "mode": mode, "settings": rcfg, "index_version": version["id"],
        "embedding": indexing.version_embed_cfg(version), "fts_query": _fts_query(terms),
        "dense_candidates": [{"chunk_id": c, "score": round(s, 4), "rank": d_rank[c]} for c, s in dense],
        "keyword_candidates": [{"chunk_id": c, "bm25": round(s, 4), "rank": k_rank[c]} for c, s in keyword],
        "final": [{"rank": r.final_rank, "chunk_id": r.chunk_id, "ref": r.ref(), "fused": round(r.fused_score, 5),
                   "dense": None if r.dense_score is None else round(r.dense_score, 4), "dense_rank": r.dense_rank,
                   "bm25": None if r.keyword_score is None else round(r.keyword_score, 4),
                   "keyword_rank": r.keyword_rank, "term_coverage": round(r.term_coverage, 2)} for r in results],
        "supported": supported, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    if log:
        db.execute("INSERT INTO retrieval_log(user_id, query, mode, debug_json, created_at) VALUES (?,?,?,?,?)",
                   (user.id, query[:2000], mode, json.dumps(debug), time.time()))
    return RetrievalResult(results, supported, debug)
