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
