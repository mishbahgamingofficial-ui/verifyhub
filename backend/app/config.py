"""Server-side configuration. Nothing here reads secrets from the frontend.

All values come from environment variables. See ../../.env.example.
No real credential is ever hard-coded in this file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _admin_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


@dataclass
class Settings:
    bot_token: str = os.environ.get("BOT_TOKEN", "")
    bot_username: str = os.environ.get("BOT_USERNAME", "")
    mini_app_url: str = os.environ.get("MINI_APP_URL", "")
    registration_url: str = os.environ.get("REGISTRATION_URL", "")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./verifyhub.db")
    admin_telegram_ids: list[int] = field(
        default_factory=lambda: _admin_ids(os.environ.get("ADMIN_TELEGRAM_IDS", ""))
    )
    project_name: str = os.environ.get("PROJECT_NAME", "VerifyHub")
    # UID validation is configurable, never hard-coded in the frontend alone.
    # Default: 6-64 chars, letters/digits, dash/underscore, must start alnum.
    uid_pattern: str = os.environ.get("UID_PATTERN", r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
    uid_pattern_description: str = os.environ.get(
        "UID_PATTERN_DESCRIPTION",
        "6–64 characters, letters, numbers, dash or underscore.",
    )
    init_data_max_age_seconds: int = int(os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400"))
    # Rate limits (per authenticated user, sliding window)
    submission_per_hour: int = int(os.environ.get("SUBMISSION_PER_HOUR", "5"))
    api_per_minute: int = int(os.environ.get("API_PER_MINUTE", "120"))
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = int(os.environ.get("PORT", "8000"))

    @property
    def uid_regex(self) -> "re.Pattern[str]":
        return re.compile(self.uid_pattern)

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_telegram_ids


def get_settings() -> Settings:
    """Fresh read so tests can override env vars between runs."""
    return Settings()
