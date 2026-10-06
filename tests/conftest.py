"""Shared fixtures: an isolated app instance (temp data dir) with admin + two users and the sample corpus.

Embeddings default to the offline hash embedder for speed; set TEST_EMBEDDING=fastembed to run the
same suite against the real local embedding model.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "sample_docs"

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

PASSWORD = "Correct-Horse-42"
EMBED = {
    "hash": {"provider": "hash", "model": "blake2b-hash-384", "dim": 384},
    "fastembed": {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dim": 384},
}[os.getenv("TEST_EMBEDDING", "hash")]


class Api:
    """Tiny helper around TestClient that carries a bearer token."""

    def __init__(self, client: TestClient, token: str | None = None):
        self.c, self.token = client, token

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, url, **kw):
        return self.c.get(url, headers=self._h(), **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers=self._h(), **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers=self._h(), **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self._h(), **kw)

    def upload(self, path: Path, visibility="private", name=None, data=None):
        content = data if data is not None else path.read_bytes()
        return self.post("/api/documents", files={"file": (name or path.name, content)}, data={"visibility": visibility})

    def ask(self, chat_id, question, mode=None):
        r = self.post(f"/api/chats/{chat_id}/ask", json={"question": question, "mode": mode})
        assert r.status_code == 200, r.text
        return r.json()


def make_settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir, storage_secret="test-secret", admin_username="admin",
                    admin_password=PASSWORD, ingest_sync=True, daily_question_quota=1000,
                    extra={"embedding": EMBED})


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    (data / "uploads").mkdir()
    settings = make_settings(data)
    client = TestClient(create_app(settings, with_ui=False))
    login = lambda u, p=PASSWORD: client.post("/api/auth/login", json={"username": u, "password": p}).json()["token"]  # noqa: E731
    admin = Api(client, login("admin"))
    for name in ("alice", "bob"):
        assert admin.post("/api/admin/users", json={"username": name, "password": PASSWORD}).status_code == 200
    alice, bob = Api(client, login("alice")), Api(client, login("bob"))
    for f in ("employee_handbook.md", "product_faq.txt", "travel_policy.docx", "quarterly_report.pdf"):
        r = admin.upload(SAMPLES / f, "shared")
        assert r.status_code == 200, r.text
    note = b"# Alice Project Notes\n\nProject Falcon launch date is 14 November 2026. Budget is 75,000 euros.\n"
    assert alice.upload(SAMPLES / "x.md", name="alice_private_notes.md", data=note).status_code == 200
    return {"client": client, "settings": settings, "admin": admin, "alice": alice, "bob": bob, "login": login}
