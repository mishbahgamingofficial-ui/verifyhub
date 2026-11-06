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
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

    from app.bot import build_telegram_application

    app = build_telegram_application("test-token-123", with_updater=False)
    assert app.updater is None  # webhook mode: no poller
    handlers = [h for group in app.handlers.values() for h in group]
    assert len(handlers) == 3
    commands = sorted(c for h in handlers if isinstance(h, CommandHandler) for c in h.commands)
    assert commands == ["start"]  # /start is the only command: the entry point
    assert sum(isinstance(h, CallbackQueryHandler) for h in handlers) == 1
    assert sum(isinstance(h, MessageHandler) for h in handlers) == 1
    assert len(app.error_handlers) == 1  # global error handler registered


def test_main_keyboard_is_colourful_and_complete():
    """The /start menu uses Bot API 9.4 button styles: blue panel, green status.

    Older Telegram clients ignore `style`, so every button also carries its
    meaning in words + emoji; at most two buttons per keyboard are styled.
    """
    from app.bot import (
        EMOJI_CHART,
        EMOJI_CROWN,
        EMOJI_DIAMOND,
        _keyboard,
    )

    buttons = [b for row in _keyboard().inline_keyboard for b in row]
    rows = _keyboard().inline_keyboard
    # Only the top actions stay inline (panel / status+predictions) —
    # the everyday tools (Signal Hub, Submit UID, Profile, Stats, Help,
    # language) moved to the persistent reply keyboard below.
    assert [len(r) for r in rows] == [1, 2]
    assert rows[0][0].text == "Verification Panel Kholo"
    assert [b.text for b in rows[1]] == ["Meri Status Dekho", "Predictions Kholo"]
    # Every inline button is premium-only: a custom emoji icon and no
    # Unicode emoji in the label (no doubled icons).
    assert all(b.icon_custom_emoji_id for b in buttons)
    assert all("🎯" not in b.text and "🚀" not in b.text and "👤" not in b.text for b in buttons)
    # No admin row anywhere in the menu: the admin panel lives only in
    # the reply keyboard, never as an inline button.
    assert all(b.text != "🛡 Admin Panel" for row in rows for b in row)
    assert all(b.callback_data != "__admin__" for row in rows for b in row)
    by_text = {b.text: b for b in buttons}
    assert by_text["Verification Panel Kholo"].style == "primary"
    assert by_text["Verification Panel Kholo"].icon_custom_emoji_id == EMOJI_CROWN
    assert by_text["Meri Status Dekho"].style == "success"
    assert by_text["Meri Status Dekho"].icon_custom_emoji_id == EMOJI_CHART
    assert by_text["Predictions Kholo"].icon_custom_emoji_id == EMOJI_DIAMOND
    assert sum(1 for b in buttons if b.style is not None) <= 2
    # The style field serialises into the Bot API payload.
    assert by_text["Verification Panel Kholo"].to_dict()["style"] == "primary"
    assert by_text["Verification Panel Kholo"].to_dict()["icon_custom_emoji_id"] == EMOJI_CROWN


def test_render_segments_counts_utf16_units():
    """_render offsets are UTF-16 code units: astral-plane emoji count as 2."""
    from telegram import MessageEntity

    from app.bot import _render

    text, entities = _render([
        ("👑", ("custom", "111")),
        (" Hi ", None),
        ("bold", "bold"),
        ("💎", ("custom", "222")),
    ])
    assert text == "👑 Hi bold💎"
    by_key = {(e.type, e.offset): e for e in entities}
    assert by_key[(MessageEntity.CUSTOM_EMOJI, 0)].length == 2
    assert by_key[(MessageEntity.CUSTOM_EMOJI, 0)].custom_emoji_id == "111"
    assert by_key[(MessageEntity.BOLD, 6)].length == 4
    assert by_key[(MessageEntity.CUSTOM_EMOJI, 10)].length == 2
    assert by_key[(MessageEntity.CUSTOM_EMOJI, 10)].custom_emoji_id == "222"


def test_welcome_and_help_texts_use_premium_emojis():
    from app.bot import (
        EMOJI_CHART_UP,
        EMOJI_CHECK,
        EMOJI_CROWN,
        EMOJI_TARGET,
        _help_text,
        _welcome_text,
    )

    text, entities = _welcome_text()
    assert "VerifyHub — ek baar, ek faisla." in text
    assert "Apna UID Verification Panel se bhejo" in text
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert customs == {EMOJI_CROWN, EMOJI_CHECK, EMOJI_TARGET, EMOJI_CHART_UP}

    text, entities = _help_text()
    assert "VerifyHub — Kaise Kaam Karta Hai" in text
    assert "Verification Panel" in text
    assert "/start" not in text and "/status" not in text  # button-only: no commands
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert EMOJI_CROWN in customs


def test_status_card_uses_entities_not_html():
    from datetime import datetime, timezone

    from app.bot import EMOJI_CHART, EMOJI_CHECK, _status_card
    from app.db import Submission, mask_uid

    text, entities = _status_card("not_submitted", None)
    assert "Aapne abhi tak UID nahi bheja hai." in text
    assert "<b>" not in text and "<code>" not in text

    row = Submission(
        registration_uid="ABCDEF123456",
        status="verified",
        submitted_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
        review_message="Looks good <b>not a tag</b>",
    )
    text, entities = _status_card("verified", row)
    assert "Verified (admin ne check kiya)" in text
    assert mask_uid("ABCDEF123456") in text
    assert "<b>not a tag</b>" in text  # literal: no HTML parsing anymore
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert EMOJI_CHART in customs
    assert EMOJI_CHECK in customs  # verified tick + progress-bar ✅

    # Badges render premium where the sets allow it (✅/⭐), plain otherwise.
    from app.bot import EMOJI_STAR, _badge_segments, _render

    text, entities = _render(_badge_segments(["✅ Verified", "⭐ Early Bird", "🏆 OG Member"]))
    assert "✅ Verified · ⭐ Early Bird · 🏆 OG Member" in text
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert EMOJI_CHECK in customs and EMOJI_STAR in customs


def test_polling_bot_builds_with_updater_and_same_handlers():
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from app.bot import build_app
    from app.bot import build_telegram_application

    polling = build_app()
    assert polling.updater is not None  # polling path intact
    webhook = build_telegram_application("test-token-123", with_updater=False)

    def _sig(a):
        return sorted(
            (type(h).__name__, tuple(sorted(h.commands)) if isinstance(h, CommandHandler) else ())
            for group in a.handlers.values() for h in group
        )

    assert _sig(polling) == _sig(webhook)
    assert len([h for group in polling.handlers.values() for h in group]) == 3


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


# ---------------- Deposit screenshot (step 2 of the flow) ----------------

def _shot():
    return "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD"


def test_submit_with_deposit_screenshot():
    uid = 555
    r = client.post("/api/submissions", headers=H(uid),
                    json={"registration_uid": "DEPO12345", "confirm_correct": True,
                          "deposit_screenshot": _shot()})
    assert r.status_code == 201, r.text
    assert r.json()["has_deposit_screenshot"] is True
    latest = client.get("/api/me/latest", headers=H(uid)).json()
    assert latest["submission"]["has_deposit_screenshot"] is True
    # Owner responses never carry the image itself, only the flag.
    assert "deposit_screenshot" not in latest["submission"]
    # Admin detail does carry it (for the later admin review work).
    sid = r.json()["id"]
    detail = client.get(f"/api/admin/submissions/{sid}", headers=H(ADMIN_ID)).json()
    assert detail["submission"]["deposit_screenshot"] == _shot()


def test_submit_with_invalid_deposit_screenshot_rejected():
    r = client.post("/api/submissions", headers=H(556),
                    json={"registration_uid": "DEPO12346", "confirm_correct": True,
                          "deposit_screenshot": "not-an-image"})
    assert r.status_code == 422


def test_submit_with_oversized_deposit_screenshot_rejected():
    big = "data:image/png;base64," + "A" * 3_000_001
    r = client.post("/api/submissions", headers=H(557),
                    json={"registration_uid": "DEPO12347", "confirm_correct": True,
                          "deposit_screenshot": big})
    assert r.status_code == 422


def test_resubmit_carries_deposit_screenshot():
    uid = 558
    r = client.post("/api/submissions", headers=H(uid),
                    json={"registration_uid": "DEPO12348", "confirm_correct": True,
                          "deposit_screenshot": _shot()})
    assert r.status_code == 201
    sid = r.json()["id"]
    # Admin asks for more info -> corrected resubmission without a fresh upload.
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "info_required", "message": "UID unclear, send a corrected one."})
    assert r.status_code == 200
    r = client.post("/api/submissions", headers=H(uid),
                    json={"registration_uid": "DEPO12349", "confirm_correct": True})
    assert r.status_code == 201
    assert r.json()["has_deposit_screenshot"] is True


# ---------------- Admin bot commands ----------------

def _fake_update(user_id, args=None):
    """Minimal PTB update/context pair for calling command handlers directly."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    # Fresh rate-limit state per fake update: the 1s guard must not leak
    # between tests that reuse the same user id.
    import app.bot as _handlers

    _handlers._last_action.clear()
    # Real PTB Message/User objects always carry these attributes; the bot
    # reads first_name (welcome) and message.entities (broadcast preview).
    message = SimpleNamespace(reply_text=AsyncMock(), entities=(), text="")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username=None),
        message=message,
    )
    context = SimpleNamespace(args=list(args or []), user_data={})
    return update, context, message


def _make_submission(telegram_id, uid, status="pending_review"):
    from app.db import Submission, User, get_session_factory

    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            user = User(telegram_id=telegram_id, username="t", first_name="T")
            db.add(user)
            db.flush()
        row = Submission(user_id=user.id, registration_uid=uid, status=status)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _submission_status(sid):
    from app.db import Submission, get_session_factory

    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        row = db.query(Submission).filter(Submission.id == sid).first()
        return (row.status, row.review_message) if row else (None, None)
    finally:
        db.close()


def test_admin_buttons_refuse_non_admin():
    import asyncio

    from app.bot import _button

    sid = _make_submission(555, "ADMINCMD1")
    update, query, message = _fake_query(USER_ID, "admin:pending")
    asyncio.run(_button(update, None))
    assert "sirf admins ke liye hai" in message.reply_text.await_args.args[0]

    update, query, message = _fake_query(USER_ID, f"admin:approve:{sid}:p")
    asyncio.run(_button(update, None))
    assert "sirf admins ke liye hai" in message.reply_text.await_args.args[0]
    assert _submission_status(sid)[0] == "pending_review"


def test_admin_pending_lists_waiting_submissions():
    import asyncio

    from app.bot import _button

    sid1 = _make_submission(556, "ADMINCMD2")
    sid2 = _make_submission(557, "ADMINCMD3")
    update, query, message = _fake_query(ADMIN_ID, "admin:pending")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert f"#{sid1}" in text and "ADMINCMD2" in text
    assert f"#{sid2}" in text and "ADMINCMD3" in text


def test_admin_approve_marks_verified_and_notifies(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot import _button
    from app.db import AuditEvent, ReviewHistory, get_session_factory

    notified = AsyncMock(return_value=True)
    monkeypatch.setattr("app.bot.notify_user", notified)
    sid = _make_submission(558, "ADMINCMD4")
    update, query, message = _fake_query(ADMIN_ID, f"admin:approve:{sid}:p")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert f"Submission #{sid} verify kar diya." in text
    assert "User ko notify kar diya gaya hai." in text
    status, review = _submission_status(sid)
    assert status == "verified" and review is None  # one-tap approve carries no custom message
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        assert db.query(ReviewHistory).filter(ReviewHistory.submission_id == sid).count() == 1
        assert db.query(AuditEvent).filter(
            AuditEvent.record_id == sid, AuditEvent.action == "status_changed"
        ).count() == 1
    finally:
        db.close()
    notified.assert_awaited_once()
    call = notified.await_args.args
    assert call[1] == 558 and call[2] == "verified" and call[3] is None


def test_admin_approve_rejects_bad_id_and_terminal():
    import asyncio

    from app.bot import _button

    update, query, message = _fake_query(ADMIN_ID, "admin:approve:999999:p")
    asyncio.run(_button(update, None))
    assert "Submission not found." in message.reply_text.await_args.args[0]

    sid = _make_submission(559, "ADMINCMD5", status="verified")
    update, query, message = _fake_query(ADMIN_ID, f"admin:approve:{sid}:p")
    asyncio.run(_button(update, None))
    assert "Cannot move a submission from verified to verified." in message.reply_text.await_args.args[0]


def test_admin_queue_buttons_apply_default_messages(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot import _button

    monkeypatch.setattr("app.bot.notify_user", AsyncMock(return_value=True))
    sid = _make_submission(560, "ADMINCMD6")
    update, query, message = _fake_query(ADMIN_ID, f"admin:reject:{sid}:p")
    asyncio.run(_button(update, None))
    assert f"Submission #{sid} reject kar diya." in query.edit_message_text.await_args.args[0]
    assert _submission_status(sid) == ("rejected", "Aapki submission admin ne approve nahi ki.")

    sid2 = _make_submission(561, "ADMINCMD7")
    update, query, message = _fake_query(ADMIN_ID, f"admin:info:{sid2}:p")
    asyncio.run(_button(update, None))
    assert "aur jaankari mangi" in query.edit_message_text.await_args.args[0]
    assert _submission_status(sid2) == (
        "info_required",
        "Aur jaankari chahiye. Verification panel me check karo.",
    )


def test_help_has_no_commands_and_mentions_admin_panel_only_for_admin():
    from app.bot import _help_text

    text, _ = _help_text(admin=True)
    assert "Admin Panel" in text
    assert "/approve" not in text and "/addadmin" not in text
    text, _ = _help_text()
    assert "Admin Panel" not in text
    assert "/start" not in text and "/status" not in text
    assert "Aapke tools" in text


# ---------------- Admin panel + instant admin alerts ----------------

def _fake_query(user_id, data):
    """Minimal PTB callback-query pair for calling _button directly."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    # Fresh rate-limit state per fake tap: the 1s guard must not leak
    # between tests that reuse the same user id.
    import app.bot as _handlers

    _handlers._last_action.clear()
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id, first_name="Test"), message=message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    return update, query, message


def test_notify_admins_posts_alert_with_action_buttons(monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    import app.bot as notif_module

    posts = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posts.append((url, json))
            return _Resp()

    monkeypatch.setattr(notif_module.httpx, "AsyncClient", _Client)
    sent = asyncio.run(notif_module.notify_admins(
        "tok", [111, 222],
        submission_id=7, registration_uid="UID123456", user_telegram_id=999,
        username="someone", first_name="Some",
        submitted_at=datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc),
        mini_app_url="https://example.test/",
    ))
    assert sent == 2 and len(posts) == 2
    assert posts[0][1]["chat_id"] == 111 and posts[1][1]["chat_id"] == 222
    assert "UID123456" in posts[0][1]["text"] and "#7" in posts[0][1]["text"]
    buttons = posts[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [
        "admin:approve:7:n", "admin:reject:7:n", "admin:info:7:n",
    ]


def test_submission_notifies_admins(monkeypatch):
    from unittest.mock import AsyncMock

    import app.main as main_module

    notified = AsyncMock()
    monkeypatch.setattr(main_module, "notify_admins", notified)
    r = client.post("/api/submissions", headers=H(666),
                    json={"registration_uid": "ADMINNOTIFY1", "confirm_correct": True})
    assert r.status_code == 201
    notified.assert_awaited_once()
    assert notified.await_args.args[1] == [ADMIN_ID]
    kwargs = notified.await_args.kwargs
    assert kwargs["registration_uid"] == "ADMINNOTIFY1"
    assert kwargs["user_telegram_id"] == 666
    assert kwargs["submission_id"] == r.json()["id"]


def test_admin_dashboard_shows_stats():
    """The panel is one slim text message: queue, verdicts, today.
    No recent-rows list, no DB ping/uptime, no inline keyboard — the
    panel's buttons live in the reply keyboard."""
    import asyncio
    from types import SimpleNamespace

    from app.bot import _button

    _make_submission(570, "DASH1")
    _make_submission(571, "DASH2")
    _make_submission(572, "DASH3", status="verified")
    update, query, message = _fake_query(ADMIN_ID, "__admin__")
    # The dashboard branch clears user_data, so it needs a real context.
    asyncio.run(_button(update, SimpleNamespace(user_data={})))
    text = message.reply_text.await_args.args[0]
    assert "Admin Panel" in text
    assert "Queue:" in text and "Verified" in text
    assert "Aaj" in text
    # Slim on purpose: no recent rows, no admin count, no DB ping.
    assert "DASH1" not in text and "DASH2" not in text and "DASH3" not in text
    assert "DB Ping" not in text and "Admins:" not in text
    # No inline keyboard attached — navigation is the reply keyboard.
    assert message.reply_text.await_args.kwargs.get("reply_markup") is None


def test_admin_approve_button_edits_notification(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot import _button

    monkeypatch.setattr("app.bot.notify_user", AsyncMock(return_value=True))
    sid = _make_submission(573, "BTN1")
    update, query, message = _fake_query(ADMIN_ID, f"admin:approve:{sid}:n")
    asyncio.run(_button(update, None))
    assert _submission_status(sid)[0] == "verified"
    text = query.edit_message_text.await_args.args[0]
    assert f"Submission #{sid} verify kar diya." in text
    # The alert is edited into the outcome with no keyboard attached.
    assert query.edit_message_text.await_args.kwargs.get("reply_markup") is None


def test_admin_button_refuses_non_admin():
    import asyncio

    from app.bot import _button

    sid = _make_submission(574, "BTN2")
    update, query, message = _fake_query(USER_ID, f"admin:approve:{sid}:n")
    asyncio.run(_button(update, None))
    assert _submission_status(sid)[0] == "pending_review"
    assert "sirf admins ke liye hai" in message.reply_text.await_args.args[0]


def test_start_shows_dashboard_to_admin():
    """An admin's /start is exactly ONE message — the slim Admin Panel —
    with the reply keyboard attached. No welcome text, no user menu,
    nothing else. A user still gets the welcome + inline menu."""
    import asyncio

    from app.bot import start
    from telegram import ReplyKeyboardMarkup

    update, context, message = _fake_update(ADMIN_ID)
    asyncio.run(start(update, context))
    assert message.reply_text.await_count == 1
    assert "Admin Panel" in message.reply_text.await_args.args[0]
    reply_kb = message.reply_text.await_args.kwargs["reply_markup"]
    assert isinstance(reply_kb, ReplyKeyboardMarkup)
    labels = [b.text for row in reply_kb.keyboard for b in row]
    assert labels == [
        "🛡 Admin Panel", "📈 Analytics",
        "📋 Pending", "🔍 Search",
        "📊 Live Stats", "👥 Admins",
        "📣 Broadcast", "📥 Export CSV",
    ]

    update, context, message = _fake_update(USER_ID)
    asyncio.run(start(update, context))
    # Users get two messages: welcome + inline menu, then the keyboard
    # note with the everyday tools in the keyboard area.
    assert message.reply_text.await_count == 2
    assert "VerifyHub" in message.reply_text.await_args_list[0].args[0]
    user_kb = message.reply_text.await_args_list[1].kwargs["reply_markup"]
    assert isinstance(user_kb, ReplyKeyboardMarkup)
    assert [b.text for row in user_kb.keyboard for b in row] == [
        "👤 Profile", "📈 Stats",
        "Help",
    ]


def test_admin_keyboard_has_panel_button_only_for_admin():
    """The main menu has no admin row for anyone — the panel lives only
    in the reply keyboard. Admins never see the inline menu at all.
    The user's everyday tools live in their own reply keyboard."""
    from app.bot import _admin_reply_keyboard, _keyboard, _user_reply_keyboard

    rows = _keyboard().inline_keyboard
    assert [len(r) for r in rows] == [1, 2]
    assert all(b.text != "🛡 Admin Panel" for row in rows for b in row)
    assert all(b.callback_data != "__admin__" for row in rows for b in row)
    # ...and the keyboard area is where the panel actually lives.
    kb = _admin_reply_keyboard()
    labels = [b.text for row in kb.keyboard for b in row]
    assert "🛡 Admin Panel" in labels
    # The user tools live in the user keyboard, not the menu.
    user_kb = _user_reply_keyboard()
    user_labels = [b.text for row in user_kb.keyboard for b in row]
    assert user_labels == [
        "👤 Profile", "📈 Stats",
        "Help",
    ]
    assert all(b.text not in ("👤 Profile", "📈 Stats")
                for row in rows for b in row)


def test_admin_reply_keyboard_is_organised():
    from app.bot import _admin_reply_keyboard

    kb = _admin_reply_keyboard()
    assert kb.resize_keyboard is True
    assert kb.one_time_keyboard is False
    rows = kb.keyboard
    assert [len(r) for r in rows] == [2, 2, 2, 2]  # panel+analytics / pending+search / stats+admins / broadcast+export
    assert [b.text for b in rows[0]] == ["🛡 Admin Panel", "📈 Analytics"]
    assert [b.text for b in rows[1]] == ["📋 Pending", "🔍 Search"]
    assert [b.text for b in rows[2]] == ["📊 Live Stats", "👥 Admins"]
    assert [b.text for b in rows[3]] == ["📣 Broadcast", "📥 Export CSV"]


def test_admin_keyboard_buttons_open_the_right_views():
    """Leaf screens show only their own actions + Back — the full main
    menu is no longer repeated underneath every screen. Back goes to
    the Admin Panel for admins, the Main Menu for users."""
    import asyncio

    from app.bot import _button

    update, query, message = _fake_query(USER_ID, "__help__")
    asyncio.run(_button(update, None))
    help_kb = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert [[b.text for b in row] for row in help_kb.inline_keyboard] == [
        ["Live Stats", "Admin se Sampark"],
        ["⬅️ Main Menu"],
    ]

    update, query, message = _fake_query(USER_ID, "__status__")
    asyncio.run(_button(update, None))
    status_kb = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert [[b.text for b in row] for row in status_kb.inline_keyboard] == [
        ["Status Refresh Karo"],
        ["⬅️ Main Menu"],
    ]

    update, query, message = _fake_query(USER_ID, "__stats__")
    asyncio.run(_button(update, None))
    stats_kb = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert [[b.text for b in row] for row in stats_kb.inline_keyboard] == [
        ["Stats Refresh Karo"],
        ["⬅️ Main Menu"],
    ]

    # An admin's leaf screens go back to the panel instead.
    update, query, message = _fake_query(ADMIN_ID, "__stats__")
    asyncio.run(_button(update, None))
    admin_stats_kb = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert [[b.text for b in row] for row in admin_stats_kb.inline_keyboard] == [
        ["Stats Refresh Karo"],
        ["⬅️ Admin Panel"],
    ]

    # And the keyboard labels open the admin views. The panel itself and
    # Live Stats reply as text only (the keyboard is already on screen);
    # Analytics, Pending and Admins have their own content keyboards.
    from app.bot import _handle_text

    _make_submission(575, "KBD1")
    for label, needle, text_only in [
        ("🛡 Admin Panel", "Admin Panel", True),
        ("📈 Analytics", "Analytics", False),
        ("📋 Pending", "KBD1", False),
        ("👥 Admins", "Platform Admins", False),
        ("📊 Live Stats", "Live Stats", True),
    ]:
        update, context, message = _fake_update(ADMIN_ID)
        message.text = label
        asyncio.run(_handle_text(update, context))
        assert needle in message.reply_text.await_args.args[0], label
        if text_only:
            assert message.reply_text.await_args.kwargs.get("reply_markup") is None, label

    # A non-admin typing a label just gets the nudge, not the admin view.
    update, context, message = _fake_update(USER_ID)
    message.text = "🛡 Admin Panel"
    asyncio.run(_handle_text(update, context))
    assert "Neeche wale buttons use karo" in message.reply_text.await_args.args[0]


def test_user_keyboard_labels_open_their_views(monkeypatch):
    """The user's reply-keyboard buttons arrive as plain text and open
    their views. (Signal Hub and Submit UID were removed from users;
    only Profile, Stats and Help remain in the keyboard.)"""
    import asyncio

    from app.bot import _handle_text

    monkeypatch.setattr(
        "app.bot.get_live",
        lambda: {"timer": "1m", "period": "2026110510952", "ends_in_seconds": 42, "last": []},
    )

    # Profile, Stats and Help.
    for label, needle in [
        ("👤 Profile", "Member Profile"),
        ("📈 Stats", "Live Stats"),
        ("Help", "Kaise Kaam Karta Hai"),
    ]:
        update, context, message = _fake_update(USER_ID)
        message.text = label
        asyncio.run(_handle_text(update, context))
        assert needle in message.reply_text.await_args.args[0], label

    # A removed label is just stray text now — the nudge, not a view.
    update, context, message = _fake_update(USER_ID)
    message.text = "🎯 Signal Hub"
    asyncio.run(_handle_text(update, context))
    assert "Neeche wale buttons use karo" in message.reply_text.await_args.args[0]


def test_search_by_telegram_id_does_not_overflow():
    """Regression: searching a Telegram id (e.g. 8995828029) used to bind
    it to submissions.id, a Postgres INTEGER, and crash with
    NumericValueOutOfRange. Telegram ids are looked up in the users
    table; only small numbers are ever tried as submission ids."""
    from app.bot import _search_submissions

    # A 10-digit id with no user behind it: no match, no crash.
    text, _kb = _search_submissions("8995828029")
    assert "se koi submission ya user nahi mila" in text

    # The same id owning a submission: found via the users table.
    _make_submission(8995828029, "SEARCHBIG1")
    text, kb = _search_submissions("8995828029")
    assert "SEARCHBIG1" in text
    assert "8995828029" in text

    # #id form with a huge number: same guard, no crash.
    text, _kb = _search_submissions("#8995828029")
    assert "SEARCHBIG1" in text

    # A real small submission id still works.
    text, _kb = _search_submissions("#1")
    assert "#1" in text


# ---------------- Live stats, badges, admin management ----------------

def test_stats_button_shows_live_numbers(monkeypatch):
    import asyncio

    from app.bot import _button
    from app.db import SignalRecord, get_session_factory

    # Seed honest accuracy data: 3 settled 1m signals, 2 right.
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        for i, correct in enumerate([True, True, False]):
            db.add(SignalRecord(timer="1m", period=f"STAT{i}", signal="BIG", correct=correct))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        "app.bot.get_live",
        lambda: {"timer": "1m", "period": "2026110510952", "ends_in_seconds": 42, "last": []},
    )
    update, query, message = _fake_query(USER_ID, "__stats__")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert "VerifyHub — Live Stats" in text
    assert "Users " in text and "Verified " in text
    assert "Signals (1m): 66.7% (2/3)" in text
    assert "Tape: ✅✅❌" in text
    assert "Live 1m: period …0952 42s me khatam" in text


def test_stats_button_without_live_period(monkeypatch):
    import asyncio

    from app.bot import _button
    from app.prediction import ProviderError

    def _boom():
        raise ProviderError("down")

    monkeypatch.setattr("app.bot.get_live", _boom)
    update, query, message = _fake_query(USER_ID, "__stats__")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert "VerifyHub — Live Stats" in text
    assert "Live 1m:" not in text


def test_badges_come_from_real_data():
    from datetime import datetime, timedelta, timezone

    from app.bot import _badges_for
    from app.db import User, get_session_factory

    # Verified + comeback: two submissions, one verified.
    _make_submission(580, "BADGE1")
    _make_submission(580, "BADGE2", status="verified")
    badges = _badges_for(580)
    assert "✅ Verified" in badges
    assert "🔁 Comeback" in badges

    # Early Bird: the very first user in this test DB.
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        first = db.query(User).order_by(User.id).first()
        first_tid = first.telegram_id
    finally:
        db.close()
    assert "⭐ Early Bird" in _badges_for(first_tid)

    # Veteran: push first_seen_at 40 days back.
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        user = db.query(User).filter(User.telegram_id == 580).first()
        user.first_seen_at = datetime.now(timezone.utc) - timedelta(days=40)
        db.commit()
    finally:
        db.close()
    assert "📅 Veteran" in _badges_for(580)

    # Unknown user: no badges at all.
    assert _badges_for(999999) == []


def test_status_button_shows_badges():
    import asyncio

    from app.bot import _button

    _make_submission(581, "BADGE3", status="verified")
    update, query, message = _fake_query(581, "__status__")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert "Badges:" in text and "✅ Verified" in text


def test_admin_add_remove_admins():
    import asyncio
    from types import SimpleNamespace

    from app.bot import _button, _handle_text

    # Non-admin cannot open the admins view.
    update, query, message = _fake_query(USER_ID, "admin:admins")
    asyncio.run(_button(update, None))
    assert "sirf admins ke liye hai" in message.reply_text.await_args.args[0]

    # Admins view lists the server admin; Add asks for an ID.
    ctx = SimpleNamespace(user_data={})
    update, query, message = _fake_query(ADMIN_ID, "admin:admins")
    asyncio.run(_button(update, ctx))
    text = query.edit_message_text.await_args.args[0]
    assert str(ADMIN_ID) in text and "(server config)" in text
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].callback_data == "admin:add"  # last row: no Back button anymore

    update, query, message = _fake_query(ADMIN_ID, "admin:add")
    asyncio.run(_button(update, ctx))
    assert ctx.user_data.get("awaiting") == "add_admin"
    assert "Telegram ID" in query.edit_message_text.await_args.args[0]

    # A non-number is rejected and the prompt stays open.
    update, context, message = _fake_update(ADMIN_ID)
    context.user_data["awaiting"] = "add_admin"
    message.text = "abc"
    asyncio.run(_handle_text(update, context))
    assert "Telegram ID nahi lag rahi" in message.reply_text.await_args.args[0]
    assert context.user_data.get("awaiting") == "add_admin"

    # Adding the server admin is refused.
    message.text = str(ADMIN_ID)
    asyncio.run(_handle_text(update, context))
    assert "pehle se admin hai" in message.reply_text.await_args.args[0]

    # Add 777, then 777 can open the admins view.
    message.text = "777"
    asyncio.run(_handle_text(update, context))
    assert "777 ab admin hai" in message.reply_text.await_args.args[0]
    update, query, message = _fake_query(777, "admin:admins")
    asyncio.run(_button(update, None))
    assert "777" in query.edit_message_text.await_args.args[0]

    # Duplicate add is refused.
    update, context, message = _fake_update(ADMIN_ID)
    context.user_data["awaiting"] = "add_admin"
    message.text = "777"
    asyncio.run(_handle_text(update, context))
    assert "already an admin" in message.reply_text.await_args.args[0]

    # A bot-added admin cannot remove the server-config admin.
    update, query, message = _fake_query(777, f"admin:remove:{ADMIN_ID}")
    asyncio.run(_button(update, None))
    assert "server config" in message.reply_text.await_args.args[0]

    # Cannot remove yourself; can remove 777.
    update, query, message = _fake_query(ADMIN_ID, f"admin:remove:{ADMIN_ID}")
    asyncio.run(_button(update, None))
    assert "khud ko remove nahi kar sakte" in message.reply_text.await_args.args[0].lower()
    update, query, message = _fake_query(ADMIN_ID, "admin:remove:777")
    asyncio.run(_button(update, None))
    assert "777 ab admin nahi raha" in query.edit_message_text.await_args.args[0]
    update, query, message = _fake_query(777, "admin:admins")
    asyncio.run(_button(update, None))
    assert "sirf admins ke liye hai" in message.reply_text.await_args.args[0]
    update, query, message = _fake_query(ADMIN_ID, "admin:remove:777")
    asyncio.run(_button(update, None))
    assert "not an admin" in message.reply_text.await_args.args[0]


def test_db_admin_can_use_api_admin_routes():
    from app.db import get_session_factory
    from app.services import add_admin

    # Add 778 straight through the service, then the API must accept them.
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        ok, error = add_admin(db, telegram_id=778, added_by=ADMIN_ID)
    finally:
        db.close()
    assert ok, error
    assert client.get("/api/admin/stats", headers=H(778)).status_code == 200
    assert client.get("/api/me", headers=H(778)).json()["is_admin"] is True
    assert client.get("/api/me", headers=H(666)).json()["is_admin"] is False
    assert client.get("/api/admin/stats", headers=H(666)).status_code == 403


# ---------------- No command menu: button-only bot ----------------

def test_command_menu_is_cleared_at_startup():
    import asyncio
    from unittest.mock import AsyncMock

    import app.bot as handlers

    bot = AsyncMock()
    asyncio.run(handlers.clear_bot_commands(bot))
    calls = bot.delete_my_commands.await_args_list
    default_calls = [c for c in calls if c.kwargs["scope"].type == "default"]
    assert len(default_calls) == 1
    chat_calls = [c for c in calls if c.kwargs["scope"].type == "chat"]
    assert any(c.kwargs["scope"].chat_id == ADMIN_ID for c in chat_calls)
    # No command is ever set anymore: the bot is button-only.
    assert bot.set_my_commands.await_count == 0


# ---------------- Bot hardening: rate limit, error handler, edit-in-place ----------------

def test_rate_limit_ignores_rapid_repeats():
    import asyncio

    import app.bot as handlers

    handlers._last_action.clear()
    assert handlers._too_fast(4242) is False
    assert handlers._too_fast(4242) is True
    assert handlers._too_fast(4343) is False
    # /start twice in a row: the second is dropped. (A user /start sends
    # two messages — welcome + keyboard note — so the count is 2, not 1.)
    handlers._last_action.clear()
    update, context, message = _fake_update(4242)
    asyncio.run(handlers.start(update, context))
    assert message.reply_text.await_count == 2
    asyncio.run(handlers.start(update, context))
    assert message.reply_text.await_count == 2


def test_error_handler_alerts_admin_once_per_window():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.bot as handlers

    handlers._last_error_alert = float("-inf")  # "never alerted", not 0.0 (which throttles on a fresh boot)
    bot = SimpleNamespace(send_message=AsyncMock())
    ctx = SimpleNamespace(bot=bot, error=ValueError("boom"))
    asyncio.run(handlers._on_error(None, ctx))
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.args[0] == ADMIN_ID
    assert "ValueError" in bot.send_message.await_args.args[1]
    asyncio.run(handlers._on_error(None, ctx))
    assert bot.send_message.await_count == 1  # throttled: no second alert


def test_help_and_status_edit_in_place():
    import asyncio

    from app.bot import _button

    update, query, message = _fake_query(USER_ID, "__help__")
    asyncio.run(_button(update, None))
    assert query.edit_message_text.await_count == 1
    assert "Kaise Kaam Karta Hai" in query.edit_message_text.await_args.args[0]
    assert message.reply_text.await_count == 0

    update, query, message = _fake_query(USER_ID, "__status__")
    asyncio.run(_button(update, None))
    assert query.edit_message_text.await_count == 1
    assert "Aapki verification status" in query.edit_message_text.await_args.args[0]
    assert message.reply_text.await_count == 0


# ---------------- Admin broadcast ----------------

def test_broadcast_counts_and_targets():
    from app.bot import _broadcast_counts, _broadcast_targets
    from app.db import Submission, User, get_session_factory

    _make_submission(595, "BC4", status="verified")
    _make_submission(596, "BC5")
    total, verified = _broadcast_counts()
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        expect_total = db.query(User).count()
        expect_verified = (
            db.query(User.telegram_id)
            .join(Submission, Submission.user_id == User.id)
            .filter(Submission.status == "verified")
            .distinct()
            .count()
        )
    finally:
        db.close()
    assert (total, verified) == (expect_total, expect_verified)
    assert 595 in _broadcast_targets(True)
    assert 596 not in _broadcast_targets(True)
    assert 595 in _broadcast_targets(False) and 596 in _broadcast_targets(False)


def test_broadcast_flow_sends_to_all_and_reports():
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot import _button, _handle_text
    from app.db import Submission, User, get_session_factory
    from telegram.error import Forbidden

    # Audience: one verified, one pending, one who blocked the bot.
    _make_submission(590, "BC1", status="verified")
    _make_submission(591, "BC2")
    _make_submission(592, "BC3")

    bot = AsyncMock()

    async def _send(chat_id, text, **kwargs):
        # PTB send_message also takes entities=/pin kwargs; accept them.
        if chat_id == 592:
            raise Forbidden("blocked")

    bot.send_message.side_effect = _send
    _, ctx, _ = _fake_update(ADMIN_ID)
    ctx.bot = bot

    # Dashboard prompt.
    update, query, message = _fake_query(ADMIN_ID, "admin:broadcast")
    asyncio.run(_button(update, ctx))
    assert ctx.user_data.get("awaiting") == "broadcast"
    assert "broadcast message" in query.edit_message_text.await_args.args[0].lower()

    # Type the message: preview with counts and send buttons.
    update, _, message = _fake_update(ADMIN_ID)
    message.text = "Hello everyone, new feature is live!"
    asyncio.run(_handle_text(update, ctx))
    assert ctx.user_data.get("broadcast_text") == "Hello everyone, new feature is live!"
    preview = message.reply_text.await_args.args[0]
    assert "Broadcast Studio Preview" in preview
    assert "Hello everyone, new feature is live!" in preview
    db = get_session_factory(os.environ["DATABASE_URL"])()
    try:
        total = db.query(User).count()
    finally:
        db.close()
    assert f"Sab: {total}" in preview
    keyboard = message.reply_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[1][0].callback_data == "admin:broadcast:all"
    assert keyboard.inline_keyboard[2][0].callback_data == "admin:broadcast:verified"

    # Send to all: the blocked user is counted, everyone else delivered.
    update, query, message = _fake_query(ADMIN_ID, "admin:broadcast:all")
    asyncio.run(_button(update, ctx))
    report = query.edit_message_text.await_args.args[0]
    assert "Broadcast poora ho gaya" in report
    assert f"Delivered: {total - 1}" in report
    assert "Blocked: 1" in report and "Failed: 0" in report
    assert "broadcast_text" not in ctx.user_data


def test_broadcast_cancel_clears_prompt():
    import asyncio
    from types import SimpleNamespace

    from app.bot import _button

    ctx = SimpleNamespace(user_data={"awaiting": "broadcast", "broadcast_text": "hi"}, bot=None)
    update, query, message = _fake_query(ADMIN_ID, "admin:cancel-broadcast")
    asyncio.run(_button(update, ctx))
    assert "awaiting" not in ctx.user_data and "broadcast_text" not in ctx.user_data
    assert "Admin Panel" in query.edit_message_text.await_args.args[0]


# ---------------- Removed features redirect to the panel ----------------

def test_removed_signal_and_submit_buttons_redirect_to_panel():
    """Signal Hub and in-chat UID submit were removed from users; old
    buttons in old messages show a Hinglish redirect, not a dead tap."""
    import asyncio

    from app.bot import _button

    update, query, message = _fake_query(USER_ID, "__signal__")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert "Signal Hub ab chat me nahi hai" in text
    assert "Prediction tab" in text

    update, query, message = _fake_query(USER_ID, "__submit_uid__")
    asyncio.run(_button(update, None))
    text = query.edit_message_text.await_args.args[0]
    assert "Chat me UID bhejna band ho gaya hai" in text
    assert "Verification Panel" in text
