"""Tests for auth, UID submission, and admin-only operations.

Run:  cd verification-platform && python -m pytest tests/ -q
      (install backend/requirements.txt first)

These tests use a real signed initData (generated with the same HMAC
construction Telegram uses), a temp SQLite file, and FastAPI's TestClient.
No test ever trusts a client-sent telegram id: the forged-id test proves
that a tampered initData is rejected even if the body claims an admin id.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

BOT_TOKEN = "test-token-123"
ADMIN_ID = 111
USER_ID = 222
OTHER_ID = 333

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
os.environ["BOT_TOKEN"] = BOT_TOKEN
os.environ["ADMIN_TELEGRAM_IDS"] = str(ADMIN_ID)
os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
os.environ["REGISTRATION_URL"] = "https://example-registration.test/register"
os.environ["MINI_APP_URL"] = "https://example.test/"

# The sandbox sets NO_PROXY with bracketed IPv6 entries (e.g. "[::1]")
# that httpx cannot parse when python-telegram-bot builds its HTTP client.
# Strip them so PTB applications can be built in tests. Production
# (Render) does not set NO_PROXY like this.
for _proxy_key in ("no_proxy", "NO_PROXY"):
    os.environ[_proxy_key] = "localhost,127.0.0.1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.main import app  # noqa: E402

client = TestClient(app)


def make_init_data(user_id, username="user"):
    user = json.dumps({"id": user_id, "username": username, "first_name": "Test"}, separators=(",", ":"))
    pairs = {"auth_date": str(int(time.time())), "user": user, "query_id": "test"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": sig})


def H(user_id):
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


def test_config_is_public_and_has_no_secrets():
    r = client.get("/api/config")
    assert r.status_code == 200
    assert "BOT_TOKEN" not in r.text
    assert r.json()["registration_url"].startswith("https://")


def test_no_initdata_is_unauthorized():
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/admin/stats").status_code == 403 or client.get("/api/admin/stats").status_code == 401


def test_forged_initdata_rejected_even_claiming_admin():
    # Tamper with the user id after signing: signature must fail.
    forged = make_init_data(USER_ID).replace(str(USER_ID), str(ADMIN_ID), 1)
    r = client.get("/api/me", headers={"X-Telegram-Init-Data": forged})
    assert r.status_code == 401


def test_user_flow_and_duplicate_protection():
    r = client.get("/api/me", headers=H(USER_ID))
    assert r.status_code == 200 and r.json()["is_admin"] is False
    # Invalid UID
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "bad uid!", "confirm_correct": True})
    assert r.status_code == 422
    # Missing confirmation
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "ABC12345", "confirm_correct": False})
    assert r.status_code == 422
    # Valid submission
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "ABC12345", "reference": "REF1", "confirm_correct": True})
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    assert r.json()["uid_masked"] != "ABC12345"  # masked for owner UI
    # Same UID again -> duplicate
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "ABC12345", "confirm_correct": True})
    assert r.status_code == 409
    # Different UID while active -> also blocked
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "XYZ99999", "confirm_correct": True})
    assert r.status_code == 409
    # Other user cannot see this submission via /me
    assert client.get("/api/me/submissions", headers=H(OTHER_ID)).json() == []
    # Latest status
    latest = client.get("/api/me/latest", headers=H(USER_ID)).json()
    assert latest["status"] == "pending_review"


def test_admin_only_and_status_transitions():
    # Create a fresh submission for a new user to avoid duplicate interference
    uid_user = 444
    r = client.post("/api/submissions", headers=H(uid_user),
                    json={"registration_uid": "ADMINTEST1", "confirm_correct": True})
    assert r.status_code == 201
    sid = r.json()["id"]
    # Non-admin cannot list / change
    assert client.get("/api/admin/submissions", headers=H(uid_user)).status_code == 403
    assert client.post(f"/api/admin/submissions/{sid}/status", headers=H(uid_user),
                       json={"to_status": "verified"}).status_code == 403
    # Admin can list and search
    rows = client.get("/api/admin/submissions?search=ADMINTEST1", headers=H(ADMIN_ID)).json()
    assert any(x["id"] == sid for x in rows)
    assert rows[0]["registration_uid"] == "ADMINTEST1"  # admin sees full UID
    # Reject without a message -> 422
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "rejected", "message": ""})
    assert r.status_code == 422
    # Valid transition to under_review, then verified
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "under_review", "internal_note": "checking"})
    assert r.status_code == 200 and r.json()["status"] == "under_review"
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "verified", "message": "Approved after review."})
    assert r.status_code == 200
    # Terminal: verified cannot go back
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "pending_review"})
    assert r.status_code == 422
    # History + audit exist
    detail = client.get(f"/api/admin/submissions/{sid}", headers=H(ADMIN_ID)).json()
    assert len(detail["history"]) == 2
    audit = client.get("/api/admin/audit", headers=H(ADMIN_ID)).json()
    assert any(a["action"] == "status_changed" and a["record_id"] == sid for a in audit)
    # User sees the new status and message
    latest = client.get("/api/me/latest", headers=H(uid_user)).json()
    assert latest["status"] == "verified"
    assert latest["submission"]["review_message"] == "Approved after review."
    # Stats are derived, not hardcoded
    stats = client.get("/api/admin/stats", headers=H(ADMIN_ID)).json()
    assert stats["total"] >= 2 and stats["by_status"]["verified"] >= 1


# ---------------- Telegram webhook ----------------

def _override_settings(monkeypatch, **overrides):
    """Override Settings for the webhook tests.

    Settings dataclass defaults are frozen at import time, so env-var
    patching and class-attribute patching do not work. Build an override
    instance with dataclasses.replace and install it both as a FastAPI
    dependency override (used by the endpoint) and as main.get_settings
    (used by get_telegram_app).
    """
    import app.main as main_module

    from dataclasses import replace

    from app.config import Settings, get_settings

    settings = replace(Settings(), **overrides)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)


def _webhook_update():
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "text": "/start",
            "chat": {"id": USER_ID, "type": "private"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
        },
    }


def test_webhook_rejects_missing_or_wrong_secret(monkeypatch):
    _override_settings(monkeypatch, telegram_webhook_secret="test-webhook-secret")
    # No header at all -> 403
    assert client.post("/telegram/webhook", json=_webhook_update()).status_code == 403
    # Wrong secret -> 403
    r = client.post("/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                    json=_webhook_update())
    assert r.status_code == 403


def test_webhook_correct_secret_processes_update(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import app.main as main_module

    _override_settings(monkeypatch, telegram_webhook_secret="test-webhook-secret")
    mock_app = MagicMock()
    mock_app.initialize = AsyncMock()
    mock_app.process_update = AsyncMock()
    mock_app.bot = MagicMock()
    mock_app.bot.defaults = None  # real bots have no defaults; a MagicMock default breaks date parsing
    monkeypatch.setattr(main_module, "get_telegram_app", lambda: mock_app)
    r = client.post("/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
                    json=_webhook_update())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    mock_app.process_update.assert_awaited_once()
    processed = mock_app.process_update.await_args.args[0]
    assert processed.update_id == 1
    assert processed.message.text == "/start"


def test_webhook_without_bot_token_is_ignored(monkeypatch):
    import app.main as main_module

    _override_settings(monkeypatch, bot_token="", telegram_webhook_secret="")
    monkeypatch.setattr(main_module, "_telegram_app", None)
    r = client.post("/telegram/webhook", json=_webhook_update())
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_telegram_application_builds_with_all_handlers():
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from app.bot_handlers import build_telegram_application

    app = build_telegram_application("test-token-123", with_updater=False)
    assert app.updater is None  # webhook mode: no poller
    handlers = [h for group in app.handlers.values() for h in group]
    assert len(handlers) == 5
    commands = sorted(c for h in handlers if isinstance(h, CommandHandler) for c in h.commands)
    assert commands == ["cancel", "help", "start", "status"]
    assert sum(isinstance(h, CallbackQueryHandler) for h in handlers) == 1


def test_polling_bot_builds_with_updater_and_same_handlers():
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from app.bot import build_app
    from app.bot_handlers import build_telegram_application

    polling = build_app()
    assert polling.updater is not None  # polling path intact
    webhook = build_telegram_application("test-token-123", with_updater=False)

    def _sig(a):
        return sorted(
            (type(h).__name__, tuple(sorted(h.commands)) if isinstance(h, CommandHandler) else ())
            for group in a.handlers.values() for h in group
        )

    assert _sig(polling) == _sig(webhook)
    assert len([h for group in polling.handlers.values() for h in group]) == 5


# ---------------- Production hardening (DB failures, webhook secret, health) ----------------

def test_health_reports_database_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "db": "ok"}


def test_health_db_down_returns_503(monkeypatch):
    import app.main as main_module
    from sqlalchemy.exc import SQLAlchemyError

    def _boom(url):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(main_module, "get_session_factory", _boom)
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["db"] == "unavailable"


def test_submission_db_failure_returns_503_not_500(monkeypatch):
    import app.db as db_module
    from sqlalchemy.exc import SQLAlchemyError

    def _boom(url):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(db_module, "get_session_factory", _boom)
    r = client.post("/api/submissions", headers=H(USER_ID),
                    json={"registration_uid": "ABCDEF", "confirm_correct": True})
    assert r.status_code == 503
    assert "Database temporarily unavailable" in r.json()["detail"]


def test_webhook_secret_unset_with_token_refuses_updates(monkeypatch):
    _override_settings(monkeypatch, bot_token="test-token-123", telegram_webhook_secret="")
    r = client.post("/telegram/webhook", json=_webhook_update())
    assert r.status_code == 503


def test_engine_pool_validates_stale_connections_and_normalizes_url():
    from app.db import make_engine

    eng = make_engine("postgres://user:pass@host:5432/dbname")
    assert eng.url.drivername == "postgresql+psycopg"
    assert eng.pool._pre_ping is True
    eng2 = make_engine("postgresql://user:pass@host:5432/dbname")
    assert eng2.pool._pre_ping is True
    eng.dispose()
    eng2.dispose()


# ---------------- Telegram ids must fit in the column (BIGINT regression) ----------------

def test_telegram_id_columns_are_bigint():
    """Telegram ids exceed the 32-bit range (e.g. 8455891912). An Integer
    column made every /api/me and /api/submissions call for such a user
    fail on Postgres with 'integer out of range' while SQLite tests
    passed silently — this assertion stops that regressing."""
    from sqlalchemy import BigInteger

    from app.db import AuditEvent, ReviewHistory, User

    assert isinstance(User.__table__.c.telegram_id.type, BigInteger)
    assert isinstance(ReviewHistory.__table__.c.admin_telegram_id.type, BigInteger)
    assert isinstance(AuditEvent.__table__.c.actor_telegram_id.type, BigInteger)


def test_large_telegram_id_can_submit():
    large_id = 8455891912  # a real Telegram id above the 32-bit range
    r = client.get("/api/me", headers=H(large_id))
    assert r.status_code == 200
    assert r.json()["telegram_id"] == large_id
    r = client.post("/api/submissions", headers=H(large_id),
                    json={"registration_uid": "ABCDEF123", "confirm_correct": True})
    assert r.status_code == 201
    assert r.json()["status"] == "pending_review"
