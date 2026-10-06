"""Application settings, loaded from environment variables / .env (never hard-coded secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    storage_secret: str
    admin_username: str
    admin_password: str
    session_hours: int = 8
    # LLM: extractive (offline, no key) | openai | anthropic | bedrock
    llm_provider: str = "extractive"
    llm_model: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    aws_region: str = "us-east-1"
    # Limits / quotas
    max_upload_mb: int = 10
    max_docs_per_user: int = 50
    daily_question_quota: int = 200
    max_concurrent_llm: int = 4
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    ingest_sync: bool = False          # tests run ingestion inline for determinism
    delete_originals: bool = True
    https_only_cookies: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    secret = os.getenv("STORAGE_SECRET", "")
    if not secret or secret == "change-me":
        raise RuntimeError("STORAGE_SECRET must be set in .env (run scripts/setup.ps1 to generate one).")
    s = Settings(
        data_dir=Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve(),
        storage_secret=secret,
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        session_hours=int(os.getenv("SESSION_HOURS", "8")),
        llm_provider=os.getenv("LLM_PROVIDER", "extractive").lower(),
        llm_model=os.getenv("LLM_MODEL", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "10")),
        max_docs_per_user=int(os.getenv("MAX_DOCS_PER_USER", "50")),
        daily_question_quota=int(os.getenv("DAILY_QUESTION_QUOTA", "200")),
        max_concurrent_llm=int(os.getenv("MAX_CONCURRENT_LLM", "4")),
        ingest_sync=_bool("INGEST_SYNC", False),
        delete_originals=_bool("DELETE_ORIGINALS", True),
        https_only_cookies=_bool("HTTPS_ONLY_COOKIES", False),
    )
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    return s


_current: Settings | None = None


def set_current(s: Settings) -> None:
    global _current
    _current = s


def current() -> Settings:
    assert _current is not None, "settings not initialised"
    return _current
