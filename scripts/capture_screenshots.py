"""Captures screenshots of every page (user + admin) into docs/screenshots for the manual/demo.

Usage: python scripts/capture_screenshots.py --admin-password ... --user-password ...
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://127.0.0.1:8080")
ap.add_argument("--admin-password", required=True)
ap.add_argument("--user-password", required=True)
a = ap.parse_args()
OUT.mkdir(parents=True, exist_ok=True)


def login(page, user, pw):
    page.goto(f"{a.base_url}/login")
    page.screenshot(path=OUT / "01_login.png")
    page.get_by_label("Username").fill(user)
    page.get_by_label("Password", exact=True).fill(pw)
    page.get_by_test_id("login").click()
    page.get_by_test_id("new-chat").wait_for()


def ask(page, q):
    n = page.get_by_test_id("assistant-message").count()
    page.locator("textarea[data-testid=question], [data-testid=question] textarea").first.fill(q)
    page.get_by_test_id("send").click()
    page.get_by_test_id("assistant-message").nth(n).wait_for()


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    login(page, "alice", a.user_password)
    page.get_by_test_id("new-chat").click()
    ask(page, "What warranty does the SolarBox 3000 include?")
    ask(page, "and its capacity?")
    page.get_by_text("Sources (").last.click()
    page.wait_for_timeout(500)
    page.screenshot(path=OUT / "02_chat_with_sources.png", full_page=True)
    ask(page, "Remember that I manage the Berlin installation team")
    page.screenshot(path=OUT / "03_memory_approval.png", full_page=True)
    page.get_by_test_id("approve-memory").click()
    page.goto(f"{a.base_url}/documents")
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "04_documents.png", full_page=True)
    page.goto(f"{a.base_url}/memory")
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "05_memory_history.png", full_page=True)
    page.goto(f"{a.base_url}/account")
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "06_mfa_setup.png", full_page=True)
    page.get_by_role("button", name="Logout").click()
    page.get_by_test_id("login").wait_for()

    login(page, "admin", a.admin_password)
    page.goto(f"{a.base_url}/admin")
    for i, tab in enumerate(["Users", "Documents & ingestion", "Profiles & chunking", "Index versions",
                             "Retrieval inspector", "Usage & errors"]):
        page.get_by_role("tab", name=tab).click()
        page.wait_for_timeout(700)
        if tab == "Retrieval inspector":
            page.get_by_label("Query").fill("SolarBox battery warranty")
            page.get_by_role("button", name="Inspect").click()
            page.wait_for_timeout(1000)
        if tab == "Profiles & chunking":
            page.get_by_label("Document").click()
            page.get_by_role("option", name="employee_handbook.md").click()
            page.get_by_role("button", name="Preview chunks").click()
            page.wait_for_timeout(1000)
        page.screenshot(path=OUT / f"{10 + i}_admin_{tab.split()[0].lower()}.png", full_page=True)
    browser.close()
print("screenshots in", OUT)
