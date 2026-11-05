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
    assert len(handlers) == 11
    commands = sorted(c for h in handlers if isinstance(h, CommandHandler) for c in h.commands)
    assert commands == ["admin", "approve", "cancel", "help", "needinfo", "pending", "predict", "reject", "start", "status"]
    assert sum(isinstance(h, CallbackQueryHandler) for h in handlers) == 1


def test_main_keyboard_is_colourful_and_complete():
    """The /start menu uses Bot API 9.4 button styles: blue panel, green status.

    Older Telegram clients ignore `style`, so every button also carries its
    meaning in words + emoji; at most two buttons per keyboard are styled.
    """
    from app.bot_handlers import (
        EMOJI_CHART,
        EMOJI_CROWN,
        EMOJI_DIAMOND,
        EMOJI_THINKING,
        _keyboard,
    )

    buttons = [b for row in _keyboard().inline_keyboard for b in row]
    by_text = {b.text: b for b in buttons}
    assert by_text["Open Verification Panel"].style == "primary"
    assert by_text["Open Verification Panel"].icon_custom_emoji_id == EMOJI_CROWN
    assert by_text["Check My Status"].style == "success"
    assert by_text["Check My Status"].icon_custom_emoji_id == EMOJI_CHART
    assert by_text["Open Predictions"].icon_custom_emoji_id == EMOJI_DIAMOND
    assert by_text["Help"].icon_custom_emoji_id == EMOJI_THINKING
    assert sum(1 for b in buttons if b.style is not None) <= 2
    # The style field serialises into the Bot API payload.
    assert by_text["Open Verification Panel"].to_dict()["style"] == "primary"
    assert by_text["Open Verification Panel"].to_dict()["icon_custom_emoji_id"] == EMOJI_CROWN


def test_render_segments_counts_utf16_units():
    """_render offsets are UTF-16 code units: astral-plane emoji count as 2."""
    from telegram import MessageEntity

    from app.bot_handlers import _render

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
    from app.bot_handlers import (
        EMOJI_CHECK,
        EMOJI_CROWN,
        EMOJI_TARGET,
        _help_text,
        _welcome_text,
    )

    text, entities = _welcome_text()
    assert "VerifyHub — one pass, one verdict." in text
    assert "Send your UID once." in text
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert customs == {EMOJI_CHECK, EMOJI_TARGET}

    text, entities = _help_text()
    assert "VerifyHub — how it works" in text
    assert "/start — open the panel" in text
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert EMOJI_CROWN in customs


def test_status_card_uses_entities_not_html():
    from datetime import datetime, timezone

    from app.bot_handlers import EMOJI_CHART, _status_card
    from app.db import Submission, mask_uid

    text, entities = _status_card("not_submitted", None)
    assert "You have not submitted a UID yet." in text
    assert "<b>" not in text and "<code>" not in text

    row = Submission(
        registration_uid="ABCDEF123456",
        status="verified",
        submitted_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
        review_message="Looks good <b>not a tag</b>",
    )
    text, entities = _status_card("verified", row)
    assert "Verified (administrator-reviewed)" in text
    assert mask_uid("ABCDEF123456") in text
    assert "<b>not a tag</b>" in text  # literal: no HTML parsing anymore
    customs = {e.custom_emoji_id for e in entities if e.type == "custom_emoji"}
    assert EMOJI_CHART in customs


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
    assert len([h for group in polling.handlers.values() for h in group]) == 11


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

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=message)
    context = SimpleNamespace(args=list(args or []))
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


def test_admin_commands_refuse_non_admin():
    import asyncio

    from app.bot_handlers import approve_cmd, pending_cmd

    sid = _make_submission(555, "ADMINCMD1")
    update, context, message = _fake_update(USER_ID)
    asyncio.run(pending_cmd(update, context))
    assert "for admins only" in message.reply_text.await_args.args[0]

    update, context, message = _fake_update(USER_ID, [str(sid)])
    asyncio.run(approve_cmd(update, context))
    assert "for admins only" in message.reply_text.await_args.args[0]
    assert _submission_status(sid)[0] == "pending_review"


def test_admin_pending_lists_waiting_submissions():
    import asyncio

    from app.bot_handlers import pending_cmd

    sid1 = _make_submission(556, "ADMINCMD2")
    sid2 = _make_submission(557, "ADMINCMD3")
    update, context, message = _fake_update(ADMIN_ID)
    asyncio.run(pending_cmd(update, context))
    text = message.reply_text.await_args.args[0]
    assert f"#{sid1}" in text and "ADMINCMD2" in text
    assert f"#{sid2}" in text and "ADMINCMD3" in text


def test_admin_approve_marks_verified_and_notifies(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot_handlers import approve_cmd
    from app.db import AuditEvent, ReviewHistory, get_session_factory

    notified = AsyncMock(return_value=True)
    monkeypatch.setattr("app.bot_handlers.notify_user", notified)
    sid = _make_submission(558, "ADMINCMD4")
    update, context, message = _fake_update(ADMIN_ID, [str(sid), "Welcome!"])
    asyncio.run(approve_cmd(update, context))
    text = message.reply_text.await_args.args[0]
    assert f"Submission #{sid} verified." in text
    assert "The user has been notified." in text
    status, review = _submission_status(sid)
    assert status == "verified" and review == "Welcome!"
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
    assert call[1] == 558 and call[2] == "verified" and call[3] == "Welcome!"


def test_admin_approve_rejects_bad_id_and_terminal():
    import asyncio

    from app.bot_handlers import approve_cmd

    update, context, message = _fake_update(ADMIN_ID, ["999999"])
    asyncio.run(approve_cmd(update, context))
    assert "Submission not found." in message.reply_text.await_args.args[0]

    update, context, message = _fake_update(ADMIN_ID, ["abc"])
    asyncio.run(approve_cmd(update, context))
    assert "not a submission id" in message.reply_text.await_args.args[0]

    update, context, message = _fake_update(ADMIN_ID, [])
    asyncio.run(approve_cmd(update, context))
    assert "Usage: /approve" in message.reply_text.await_args.args[0]

    sid = _make_submission(559, "ADMINCMD5", status="verified")
    update, context, message = _fake_update(ADMIN_ID, [str(sid)])
    asyncio.run(approve_cmd(update, context))
    assert "Cannot move a submission from verified to verified." in message.reply_text.await_args.args[0]


def test_admin_reject_requires_message_and_needinfo(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot_handlers import needinfo_cmd, reject_cmd

    monkeypatch.setattr("app.bot_handlers.notify_user", AsyncMock(return_value=True))
    sid = _make_submission(560, "ADMINCMD6")
    update, context, message = _fake_update(ADMIN_ID, [str(sid)])
    asyncio.run(reject_cmd(update, context))
    assert "review message is required" in message.reply_text.await_args.args[0]
    assert _submission_status(sid)[0] == "pending_review"

    update, context, message = _fake_update(ADMIN_ID, [str(sid), "Not eligible"])
    asyncio.run(reject_cmd(update, context))
    assert f"Submission #{sid} rejected." in message.reply_text.await_args.args[0]
    assert _submission_status(sid)[0] == "rejected"

    sid2 = _make_submission(561, "ADMINCMD7")
    update, context, message = _fake_update(ADMIN_ID, [str(sid2), "Please send a clearer screenshot"])
    asyncio.run(needinfo_cmd(update, context))
    assert "needing more information" in message.reply_text.await_args.args[0]
    assert _submission_status(sid2) == ("info_required", "Please send a clearer screenshot")


def test_help_lists_admin_commands_only_for_admin():
    from app.bot_handlers import _help_text

    text, _ = _help_text(admin=True)
    assert "/approve <id>" in text and "/pending" in text
    text, _ = _help_text()
    assert "/approve" not in text and "/pending" not in text


# ---------------- Admin panel + instant admin alerts ----------------

def _fake_query(user_id, data):
    """Minimal PTB callback-query pair for calling _button directly."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), message=message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    return update, query, message


def test_notify_admins_posts_alert_with_action_buttons(monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    import app.notifications as notif_module

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
    import asyncio

    from app.bot_handlers import admin_cmd

    _make_submission(570, "DASH1")
    _make_submission(571, "DASH2")
    _make_submission(572, "DASH3", status="verified")
    update, context, message = _fake_update(ADMIN_ID)
    asyncio.run(admin_cmd(update, context))
    text = message.reply_text.await_args.args[0]
    assert "Admin Panel" in text
    assert "Pending:" in text and "Verified:" in text
    assert "DASH1" in text and "DASH2" in text and "DASH3" in text
    keyboard = message.reply_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "admin:pending"


def test_admin_approve_button_edits_notification(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.bot_handlers import _button

    monkeypatch.setattr("app.bot_handlers.notify_user", AsyncMock(return_value=True))
    sid = _make_submission(573, "BTN1")
    update, query, message = _fake_query(ADMIN_ID, f"admin:approve:{sid}:n")
    asyncio.run(_button(update, None))
    assert _submission_status(sid)[0] == "verified"
    text = query.edit_message_text.await_args.args[0]
    assert f"Submission #{sid} verified." in text
    assert "reply_markup" not in query.edit_message_text.await_args.kwargs


def test_admin_button_refuses_non_admin():
    import asyncio

    from app.bot_handlers import _button

    sid = _make_submission(574, "BTN2")
    update, query, message = _fake_query(USER_ID, f"admin:approve:{sid}:n")
    asyncio.run(_button(update, None))
    assert _submission_status(sid)[0] == "pending_review"
    assert "for admins only" in message.reply_text.await_args.args[0]


def test_start_shows_dashboard_to_admin():
    import asyncio

    from app.bot_handlers import start

    update, context, message = _fake_update(ADMIN_ID)
    asyncio.run(start(update, context))
    assert message.reply_text.await_count == 2
    assert "Admin Panel" in message.reply_text.await_args_list[1].args[0]

    update, context, message = _fake_update(USER_ID)
    asyncio.run(start(update, context))
    assert message.reply_text.await_count == 1


def test_admin_keyboard_has_panel_button_only_for_admin():
    from app.bot_handlers import _keyboard

    admin_buttons = [b for row in _keyboard(admin=True).inline_keyboard for b in row]
    assert any(b.text == "🛡 Admin Panel" and b.callback_data == "__admin__" for b in admin_buttons)
    user_buttons = [b for row in _keyboard().inline_keyboard for b in row]
    assert all(b.text != "🛡 Admin Panel" for b in user_buttons)
