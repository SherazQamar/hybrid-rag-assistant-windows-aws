"""Memory within a chat, across chats, across logout and across an application restart."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import Api, make_settings


def test_within_chat_follow_up_uses_context(env):
    alice = env["alice"]
    chat = alice.post("/api/chats").json()["id"]
    alice.ask(chat, "What does the SolarBox 3000 cost?")
    follow = alice.ask(chat, "and its capacity?")  # too short on its own; needs the previous turn
    assert "13.5 kwh" in follow["answer"].lower()


def test_memory_requires_approval_and_is_recalled_in_new_chat(env):
    bob = env["bob"]
    chat = bob.post("/api/chats").json()["id"]
    res = bob.ask(chat, "Remember that my preferred airline is Lufthansa")
    assert len(res["pending_memories"]) == 1
    mid = res["pending_memories"][0]["id"]

    new_chat = bob.post("/api/chats").json()["id"]
    before = bob.ask(new_chat, "Which airline do I prefer?")
    assert "lufthansa" not in before["answer"].lower()  # pending memory is NOT used

    assert bob.post(f"/api/memories/{mid}/approve").status_code == 200
    after = bob.ask(bob.post("/api/chats").json()["id"], "Which airline do I prefer?")
    assert "lufthansa" in after["answer"].lower()


def test_memory_survives_logout_and_restart(env):
    client, login = env["client"], env["login"]
    bob = Api(client, login("bob"))
    bob.post("/api/auth/logout")
    # simulate an application restart / Windows reboot: brand-new app object on the same database
    restarted = TestClient(create_app(make_settings(env["settings"].data_dir), with_ui=False))
    token = restarted.post("/api/auth/login", json={"username": "bob", "password": "Correct-Horse-42"}).json()["token"]
    bob2 = Api(restarted, token)
    res = bob2.ask(bob2.post("/api/chats").json()["id"], "Which airline do I prefer?")
    assert "lufthansa" in res["answer"].lower()
    env["bob"].token = token


def test_memory_is_private_and_deletable(env):
    alice, bob = env["alice"], env["bob"]
    res = alice.ask(alice.post("/api/chats").json()["id"], "Which airline do I prefer?")
    assert "lufthansa" not in res["answer"].lower()  # bob's memory never leaks to alice
    mem = next(m for m in bob.get("/api/memories").json() if "Lufthansa" in m["content"])
    assert alice.delete(f"/api/memories/{mem['id']}").status_code == 404
    assert bob.delete(f"/api/memories/{mem['id']}").status_code == 200
    res = bob.ask(bob.post("/api/chats").json()["id"], "Which airline do I prefer?")
    assert "lufthansa" not in res["answer"].lower()


def test_history_can_be_inspected_and_deleted(env):
    alice = env["alice"]
    chat = alice.post("/api/chats").json()["id"]
    alice.ask(chat, "What is the meal per diem?")
    msgs = alice.get(f"/api/chats/{chat}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["sources"]
    assert alice.delete(f"/api/chats/{chat}").status_code == 200
    assert alice.get(f"/api/chats/{chat}/messages").status_code == 404
