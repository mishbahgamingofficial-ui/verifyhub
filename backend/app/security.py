"""Telegram Web App initData validation — the security core of the Mini App.

Implements Telegram's documented check (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
  data_check_string = sorted "key=value" lines (excluding `hash`), joined by \n
  secret_key = HMAC-SHA256(key=b"WebAppData", msg=bot_token)
  check = HMAC-SHA256(key=secret_key, msg=data_check_string)
  compare check (hex) to `hash` in constant time.

Never trust a telegram id, username, or is_admin flag sent in a JSON body.
Every authenticated request must carry the signed initData in the
X-Telegram-Init-Data header, and this module is the only place that
turns it into an identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


@dataclass
class TelegramIdentity:
    telegram_id: int
    username: str | None = None
    first_name: str | None = None


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> TelegramIdentity:
    if not bot_token:
        raise InitDataError("Server is not configured with a bot token.")
    if not init_data:
        raise InitDataError("Missing Telegram initData.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData has no hash.")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise InitDataError("initData signature is invalid.")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("initData auth_date is invalid.") from None
    if auth_date <= 0 or (time.time() - auth_date) > max_age_seconds:
        raise InitDataError("initData has expired. Reopen the Mini App from Telegram.")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise InitDataError("initData user payload is invalid.") from None
    if not isinstance(user, dict) or "id" not in user:
        raise InitDataError("initData has no user id.")
    try:
        telegram_id = int(user["id"])
    except (TypeError, ValueError):
        raise InitDataError("initData user id is invalid.") from None
    return TelegramIdentity(
        telegram_id=telegram_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
    )
