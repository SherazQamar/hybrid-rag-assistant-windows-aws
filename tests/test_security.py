"""Security tests via direct HTTP requests (browser-level tests live in tests/e2e)."""
import pyotp
import pytest

from app import db
from tests.conftest import PASSWORD, SAMPLES, Api


# ---------- authentication & sessions ----------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer forged-token"}, {"Authorization": "Basic YWRtaW4="}])
def test_requests_without_valid_session_are_rejected(env, headers):
    assert env["client"].get("/api/chats", headers=headers).status_code == 401


def test_logout_invalidates_token(env):
    api = Api(env["client"], env["login"]("alice"))
    assert api.get("/api/me").status_code == 200
    api.post("/api/auth/logout")
    assert api.get("/api/me").status_code == 401


def test_expired_session_rejected(env):
    api = Api(env["client"], env["login"]("alice"))
    db.execute("UPDATE sessions SET expires_at = 0 WHERE user_id = (SELECT id FROM users WHERE username='alice') "
               "AND created_at = (SELECT MAX(created_at) FROM sessions)")
    assert api.get("/api/me").status_code == 401


def test_deactivation_kills_sessions_and_blocks_login(env):
    admin, client = env["admin"], env["client"]
    uid = admin.post("/api/admin/users", json={"username": "dave", "password": PASSWORD}).json()["id"]
    dave = Api(client, env["login"]("dave"))
    assert admin.patch(f"/api/admin/users/{uid}", json={"active": False}).status_code == 200
    assert dave.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"username": "dave", "password": PASSWORD}).status_code == 401


def test_bruteforce_lockout(env):
    client = env["client"]
    env["admin"].post("/api/admin/users", json={"username": "erin", "password": PASSWORD})
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "erin", "password": "wrong-password"}).status_code == 401
    # even the correct password is refused while locked out
    assert client.post("/api/auth/login", json={"username": "erin", "password": PASSWORD}).status_code == 429


def test_mfa_required_once_enabled(env):
    from app import security
    client = env["client"]
    uid = env["admin"].post("/api/admin/users", json={"username": "carol", "password": PASSWORD}).json()["id"]
    secret = security.new_totp_secret()
    security.enable_mfa(uid, secret, pyotp.TOTP(secret).now())
    assert client.post("/api/auth/login", json={"username": "carol", "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "carol", "password": PASSWORD, "totp": "000000"}).status_code == 401
    ok = client.post("/api/auth/login", json={"username": "carol", "password": PASSWORD, "totp": pyotp.TOTP(secret).now()})
    assert ok.status_code == 200


# ---------- authorization & isolation (modified requests) ----------

@pytest.mark.parametrize("method,url", [("get", "/api/admin/users"), ("get", "/api/admin/usage"),
                                         ("post", "/api/admin/reindex"), ("get", "/api/admin/profiles")])
def test_non_admin_forbidden(env, method, url):
    assert getattr(env["bob"], method)(url).status_code == 403


def test_cross_user_access_by_id_tampering(env):
    alice, bob = env["alice"], env["bob"]
    chat = alice.post("/api/chats").json()["id"]
    alice.ask(chat, "What is the meal per diem?")
    assert bob.get(f"/api/chats/{chat}/messages").status_code == 404
    assert bob.post(f"/api/chats/{chat}/ask", json={"question": "hi"}).status_code == 404
    assert bob.delete(f"/api/chats/{chat}").status_code == 404
    private = next(d for d in alice.get("/api/documents").json() if d["filename"] == "alice_private_notes.md")
    assert private["id"] not in [d["id"] for d in bob.get("/api/documents").json()]
    assert bob.delete(f"/api/documents/{private['id']}").status_code == 404


def test_private_documents_isolated_at_retrieval_time(env):
    alice, bob = env["alice"], env["bob"]
    q = "When is the Project Falcon launch date?"
    assert "14 november" in alice.ask(alice.post("/api/chats").json()["id"], q)["answer"].lower()
    res = bob.ask(bob.post("/api/chats").json()["id"], q)
    assert res["supported"] is False and "falcon" not in res["answer"].lower()


def test_users_cannot_publish_shared_documents(env):
    r = env["bob"].upload(SAMPLES / "x.md", visibility="shared", name="x.md", data=b"# hello world text")
    assert r.status_code == 403


def test_deleted_document_leaves_retrieval(env):
    alice = env["alice"]
    data = b"# Temp\n\nThe Zephyr warehouse code is ZX-4471 and opens at 7am.\n"
    doc_id = alice.upload(SAMPLES / "x.md", name="temp.md", data=data).json()["id"]
    q = "What is the Zephyr warehouse code?"
    assert "zx-4471" in alice.ask(alice.post("/api/chats").json()["id"], q)["answer"].lower()
    assert alice.delete(f"/api/documents/{doc_id}").status_code == 200
    assert db.one("SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (doc_id,))["n"] == 0
    assert "zx-4471" not in alice.ask(alice.post("/api/chats").json()["id"], q)["answer"].lower()


# ---------- injection, uploads, secrets ----------

@pytest.mark.parametrize("q", ["'; DROP TABLE users; --", '" OR 1=1 --', 'NEAR("a" "b") AND *', "../../etc/passwd"])
def test_sql_and_fts_injection_are_inert(env, q):
    r = env["alice"].post("/api/search", json={"query": q, "mode": "hybrid"})
    assert r.status_code == 200
    assert db.one("SELECT COUNT(*) AS n FROM users")["n"] >= 3


@pytest.mark.parametrize("name,data,status", [
    ("evil.exe", b"MZ\x90\x00", 400),
    ("fake.pdf", b"<html><script>alert(1)</script></html>", 400),
    ("fake.docx", b"PK\x03\x04 not really a zip", 400),
    ("binary.txt", b"\x00\x01\x02 binary", 400),
    ("empty.md", b"", 400),
])
def test_unsafe_uploads_rejected(env, name, data, status):
    assert env["alice"].upload(SAMPLES / "x", name=name, data=data).status_code == status


def test_oversize_upload_rejected(env):
    big = b"a" * (env["settings"].max_upload_mb * 1024 * 1024 + 10)
    assert env["alice"].upload(SAMPLES / "x", name="big.txt", data=big).status_code == 400


def test_path_traversal_filename_is_neutralised(env):
    r = env["alice"].upload(SAMPLES / "x", name="..\\..\\..\\Windows\\evil.md", data=b"# Harmless traversal test text")
    assert r.status_code == 200
    row = db.one("SELECT filename, stored_name FROM documents WHERE id = ?", (r.json()["id"],))
    assert row["filename"] == "evil.md" and row["stored_name"].endswith(".bin") and "/" not in row["stored_name"]


@pytest.mark.parametrize("path", ["/.env", "/app/config.py", "/data/app.db", "/.git/config", "/%2e%2e/.env",
                                  "/static/../.env", "/docs", "/openapi.json"])
def test_source_and_secrets_not_served(env, path):
    assert env["client"].get(path).status_code in (404, 405)


def test_security_headers(env):
    r = env["client"].get("/healthz")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"


def test_xss_payload_stored_as_plain_text(env):
    alice = env["alice"]
    chat = alice.post("/api/chats").json()["id"]
    alice.ask(chat, "<script>alert('x')</script> warranty?")
    msgs = alice.get(f"/api/chats/{chat}/messages")
    assert msgs.headers["content-type"].startswith("application/json")
    assert msgs.json()[0]["content"] == "<script>alert('x')</script> warranty?"  # rendered as text by the UI


# ---------- agent permissions vs prompt injection ----------

def test_prompt_injection_in_document_cannot_trigger_tools(env):
    admin, bob = env["admin"], env["bob"]
    assert admin.upload(SAMPLES / "malicious_injection.md", "shared").status_code == 200
    before = len(bob.get("/api/memories").json())
    res = bob.ask(bob.post("/api/chats").json()["id"], "What is the vendor portal master password for onboarding?")
    assert len(bob.get("/api/memories").json()) == before  # no memory written by the document
    assert all(r["origin"] != "user" for r in res["rejected_tool_calls"])
    assert "falcon" not in res["answer"].lower()  # "reveal every user's documents" had no effect


# ---------- quotas ----------

def test_daily_question_quota(env):
    from app.config import current
    s = current()
    old, s.daily_question_quota = s.daily_question_quota, 0
    try:
        r = env["alice"].post(f"/api/chats/{env['alice'].post('/api/chats').json()['id']}/ask", json={"question": "hi"})
        assert r.status_code == 429
    finally:
        s.daily_question_quota = old
