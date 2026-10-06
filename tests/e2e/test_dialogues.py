"""Browser dialogues (single user) and parallel simulated users against the real frontend.

  pytest tests/e2e -m e2e -k "not parallel"     # JSON dialogues + browser security checks
  E2E_USERS=5 pytest tests/e2e -m e2e -k parallel -s
"""
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e.browser_user import BrowserUser
from tests.e2e.conftest import BASE_URL, DIALOGUES, PASSWORD

pytestmark = pytest.mark.e2e
REPORTS = Path(__file__).resolve().parents[2] / "reports"


def run_dialogue(user: BrowserUser, dialogue: dict) -> list[str]:
    """Returns failed assertions (empty = pass). Asserts facts and sources, not exact wording."""
    failures = []
    user.new_chat()
    for step in dialogue["steps"]:
        if step.get("new_chat"):
            user.new_chat()
        rec = user.ask(step["ask"], DIALOGUES["settings"]["answer_timeout_ms"])
        text = rec["answer"].lower().replace("\n", " ")
        if step.get("expect_refusal") and "could not find" not in text:
            failures.append(f"{step['ask']!r}: expected refusal, got {rec['answer'][:120]!r}")
        for fact in step.get("expect_facts", []):
            if fact.lower() not in text:
                failures.append(f"{step['ask']!r}: missing fact {fact!r}")
        for src in step.get("expect_sources", []):
            if not any(s.split("]", 1)[-1].strip().startswith(src) for s in rec["sources"]):
                failures.append(f"{step['ask']!r}: missing source {src!r} in {rec['sources']}")
        if step.get("expect_pending_memory"):
            if user.page.get_by_test_id("approve-memory").count() == 0:
                failures.append(f"{step['ask']!r}: no memory approval prompt")
            elif step.get("approve"):
                user.approve_memory()
    return failures


@pytest.mark.parametrize("dialogue", DIALOGUES["dialogues"], ids=lambda d: d["name"])
def test_dialogue_in_browser(page, dialogue):
    user = BrowserUser(page, BASE_URL, "alice", PASSWORD)
    user.login()
    failures = run_dialogue(user, dialogue)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"transcript_{dialogue['name']}.json").write_text(json.dumps(user.transcript, indent=2))
    assert not failures, "\n".join(failures)


def test_login_rejects_bad_password(page):
    page.goto(f"{BASE_URL}/login")
    page.get_by_label("Username").fill("bob")
    page.get_by_label("Password", exact=True).fill("wrong-password-x")
    page.get_by_test_id("login").click()
    page.wait_for_timeout(1500)
    assert page.url.endswith("/login")


def test_admin_page_forbidden_for_user(page):
    BrowserUser(page, BASE_URL, "bob", PASSWORD).login()
    page.goto(f"{BASE_URL}/admin")
    assert page.get_by_text("403 - admin role required").is_visible()


def test_unauthenticated_browser_redirected_to_login(page):
    page.goto(f"{BASE_URL}/documents")
    page.wait_for_url("**/login", timeout=10000)


def _simulated_user(idx: int, exchanges: int, think_s: float) -> dict:
    """One user in its own browser + context: logs in, runs `exchanges` questions with think time."""
    steps = [s for d in DIALOGUES["dialogues"][:2] for s in d["steps"]]
    name = f"{os.getenv('E2E_USER_PREFIX', 'user')}{idx:02d}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        user = BrowserUser(browser.new_context().new_page(), BASE_URL, name, PASSWORD)
        start = time.time()
        user.login()
        user.new_chat()
        failures = []
        for i in range(exchanges):
            step = steps[i % len(steps)]
            rec = user.ask(step["ask"])
            failures += [f"{name}: {step['ask']!r} missing {f!r}" for f in step["expect_facts"]
                         if f.lower() not in rec["answer"].lower()]
            time.sleep(think_s)
        end = time.time()
        browser.close()
    return {"user": name, "start": start, "end": end, "failures": failures, "transcript": user.transcript}


def test_parallel_users():
    n = int(os.getenv("E2E_USERS", "5"))
    exchanges = int(os.getenv("E2E_EXCHANGES", "10"))
    think = float(os.getenv("E2E_THINK_S", "1"))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda i: _simulated_user(i, exchanges, think), range(1, n + 1)))
    wall = time.time() - t0
    lat = sorted(r["latency_s"] for res in results for r in res["transcript"])
    overlap = min(r["end"] for r in results) - max(r["start"] for r in results)
    report = {
        "users": n, "exchanges_per_user": exchanges, "think_time_s": think, "wall_s": round(wall, 1),
        "requests": len(lat), "throughput_per_s": round(len(lat) / wall, 2),
        "latency_median_s": round(statistics.median(lat), 3),
        "latency_p95_s": round(lat[int(0.95 * (len(lat) - 1))], 3),
        "all_users_active_together_s": round(max(0.0, overlap), 1),
        "failures": [f for r in results for f in r["failures"]],
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"parallel_{n}_users.json").write_text(json.dumps({"summary": report, "results": results}, indent=2))
    print(json.dumps(report, indent=2))
    assert not report["failures"], report["failures"]
