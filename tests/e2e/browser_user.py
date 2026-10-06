"""A simulated user driving the real frontend with Python Playwright."""
from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect


class BrowserUser:
    def __init__(self, page: Page, base_url: str, username: str, password: str):
        self.page, self.base, self.username, self.password = page, base_url, username, password
        self.transcript: list[dict] = []

    def login(self) -> None:
        p = self.page
        p.goto(f"{self.base}/login")
        p.get_by_label("Username").fill(self.username)
        p.get_by_label("Password", exact=True).fill(self.password)
        p.get_by_test_id("login").click()
        expect(p.get_by_test_id("new-chat")).to_be_visible(timeout=15000)

    def new_chat(self) -> None:
        self.page.get_by_test_id("new-chat").click()
        expect(self.page.get_by_test_id("assistant-message")).to_have_count(0, timeout=10000)

    def ask(self, question: str, timeout_ms: int = 60000) -> dict:
        p = self.page
        before = p.get_by_test_id("assistant-message").count()
        t0 = time.perf_counter()
        p.locator("textarea[data-testid=question], [data-testid=question] textarea").first.fill(question)
        p.get_by_test_id("send").click()
        expect(p.get_by_test_id("assistant-message")).to_have_count(before + 1, timeout=timeout_ms)
        latency = time.perf_counter() - t0
        msg = p.get_by_test_id("assistant-message").nth(before)
        answer = msg.inner_text()
        sources = []
        toggle = msg.get_by_text(re.compile(r"Sources \(\d+\)"))
        if toggle.count():
            toggle.click()
            expect(msg.get_by_test_id("source-ref").first).to_be_visible()
            sources = [s.inner_text() for s in msg.get_by_test_id("source-ref").all()]
        rec = {"user": self.username, "question": question, "answer": answer, "sources": sources,
               "latency_s": round(latency, 3)}
        self.transcript.append(rec)
        return rec

    def approve_memory(self) -> None:
        self.page.get_by_test_id("approve-memory").first.click()
        expect(self.page.get_by_test_id("approve-memory")).to_have_count(0, timeout=10000)
