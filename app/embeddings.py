"""Embedding providers. Every vector is L2-normalised so dot product == cosine similarity.

- fastembed : BAAI/bge-small-en-v1.5 (384-d) running locally on CPU via ONNX - no API key, no cost.
- hash      : deterministic feature hashing (offline fallback for tests; lexical, NOT semantic).
- openai    : text-embedding-3-small (1536-d) via HTTPS.
- bedrock   : amazon.titan-embed-text-v2:0 via boto3 (uses the Windows host's AWS credentials).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

import numpy as np

_models: dict[str, object] = {}
_lock = threading.Lock()


def _normalise(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.where(norms == 0, 1, norms)).astype(np.float32)


def _hash_embed(texts: list[str], dim: int) -> np.ndarray:
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for row, t in enumerate(texts):
        words = re.findall(r"\w+", t.lower())
        feats = words + [w[i:i + 3] for w in words for i in range(max(len(w) - 2, 1))]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            out[row, h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    return _normalise(out)


def _fastembed(texts: list[str], model: str) -> np.ndarray:
    with _lock:
        if model not in _models:
            from fastembed import TextEmbedding
            _models[model] = TextEmbedding(model_name=model)
    return _normalise(np.array(list(_models[model].embed(texts)), dtype=np.float32))  # type: ignore[attr-defined]


def _openai(texts: list[str], model: str, api_key: str) -> np.ndarray:
    import httpx

    r = httpx.post("https://api.openai.com/v1/embeddings", timeout=60,
                   headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "input": texts})
    r.raise_for_status()
    return _normalise(np.array([d["embedding"] for d in r.json()["data"]], dtype=np.float32))


def _bedrock(texts: list[str], model: str, region: str) -> np.ndarray:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    vecs = []
    for t in texts:
        resp = client.invoke_model(modelId=model, body=json.dumps({"inputText": t}))
        vecs.append(json.loads(resp["body"].read())["embedding"])
    return _normalise(np.array(vecs, dtype=np.float32))


def embed(texts: list[str], cfg: dict, settings) -> np.ndarray:
    """cfg = {"provider", "model", "dim"} - always taken from the index version for queries,
    so query vectors can never be compared against vectors from a different model."""
    if not texts:
        return np.zeros((0, cfg["dim"]), dtype=np.float32)
    p = cfg["provider"]
    if p == "hash":
        vecs = _hash_embed(texts, cfg["dim"])
    elif p == "fastembed":
        vecs = _fastembed(texts, cfg["model"])
    elif p == "openai":
        vecs = _openai(texts, cfg["model"], settings.openai_api_key)
    elif p == "bedrock":
        vecs = _bedrock(texts, cfg["model"], settings.aws_region)
    else:
        raise ValueError(f"Unknown embedding provider {p}")
    if vecs.shape[1] != cfg["dim"]:
        raise ValueError(f"Embedding dimension mismatch: model returned {vecs.shape[1]}, profile says {cfg['dim']}")
    return vecs


def default_config() -> dict:
    """fastembed if importable, else the offline hash embedder."""
    try:
        import fastembed  # noqa: F401
        return {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dim": 384}
    except ImportError:
        return {"provider": "hash", "model": "blake2b-hash-384", "dim": 384}
