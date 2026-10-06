"""Browser-test settings. Targets a RUNNING instance (local, or the public HTTPS URL through AWS).

Env: BASE_URL, E2E_USER_PASSWORD, E2E_USER_PREFIX (default 'user'), E2E_USERS, E2E_EXCHANGES, E2E_THINK_S.
"""
import json
import os
from pathlib import Path

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080")
PASSWORD = os.getenv("E2E_USER_PASSWORD", "User-Demo-2026!")
DIALOGUES = json.loads((Path(__file__).parent.parent / "dialogues" / "knowledge_assistant.json").read_text())
