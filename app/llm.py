"""LLM providers. `extractive` works fully offline (no key, no cost) by quoting the best-matching
sentences from retrieved chunks with citations; the others call a hosted model over HTTPS."""
from __future__ import annotations

import json
import re

import httpx

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "bedrock": "anthropic.claude-3-haiku-20240307-v1:0",
}


def provider_info(settings) -> dict:
    p = settings.llm_provider
    return {"provider": p, "model": settings.llm_model or DEFAULT_MODELS.get(p, "extractive-v1")}


def generate(system: str, messages: list[dict], settings) -> str:
    p, model = settings.llm_provider, settings.llm_model or DEFAULT_MODELS.get(settings.llm_provider, "")
    if p == "openai":
        r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=120,
                       headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                       json={"model": model, "temperature": 0.1,
                             "messages": [{"role": "system", "content": system}] + messages})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    if p == "anthropic":
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=120,
                       headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
                       json={"model": model, "max_tokens": 1024, "temperature": 0.1, "system": system,
                             "messages": messages})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    if p == "bedrock":
        import boto3

        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        resp = client.converse(modelId=model, system=[{"text": system}],
                               messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
                               inferenceConfig={"maxTokens": 1024, "temperature": 0.1})
        return "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
    raise ValueError(f"Provider {p} has no generate(); use extractive_answer")


def extractive_answer(question_terms: list[str], sources: list, memories: list[dict],
                      context_terms: list[str] = ()) -> str:
    """Offline answer: the best sentences by query-term overlap, each with its [n] source marker.
    Terms of the current question weigh double; terms carried from earlier turns weigh single."""
    def primary(sentence: str) -> int:
        low = sentence.lower()
        return sum(t[:5] in low for t in question_terms)

    def score(sentence: str) -> float:  # current-question terms dominate; earlier-turn terms break ties
        low = sentence.lower()
        return 10 * primary(sentence) + sum(t[:5] in low for t in context_terms)

    lines: list[str] = []
    for m in [m for m in memories if score(m["content"]) > 0][:3]:
        lines.append(f"From your saved memory: {m['content']}")
    picked: list[tuple[float, int, str]] = []
    for n, src in enumerate(sources, start=1):
        carry = 0.0  # FAQ-style text: a matching question boosts the sentence that answers it
        for sent in re.split(r"(?<=[.!?])\s+|\n+", src.text):
            sent = sent.strip(" #*-\t")
            if len(sent) <= 15:
                continue
            if sent.endswith("?"):
                carry = 20 * primary(sent)
                continue
            if (s := score(sent) + carry) > 0:
                picked.append((s - n * 0.01, n, sent))
            carry = 0.0
    picked.sort(key=lambda x: -x[0])
    best = picked[0][0] if picked else 0
    for _, n, sent in [p for p in picked if p[0] >= best * 0.8][:3]:
        lines.append(f"{sent} [{n}]")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
