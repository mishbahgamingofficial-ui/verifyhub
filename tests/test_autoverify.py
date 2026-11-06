"""Tests for automatic verification (app.autoverify).

The client tests use httpx.MockTransport so no real DmPlay/DmFirst
call is made. The flow tests use a fake client (same interface as
DmPlayClient) against a temporary SQLite database.
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import date

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import autoverify
from app.autoverify import AutoVerifyError, DmPlayClient, check_uid
from app.config import Settings
from app.db import Submission, User, get_session_factory


# ---------------- mock transport helpers ----------------

def _signed_body_ok(body: dict) -> bool:
    """Recompute the MD5 signature the way the server does and compare."""
    to_sign = {k: v for k, v in body.items()
               if k not in ("signature", "timestamp") and v not in (None, "")}
    payload = json.dumps(dict(sorted(to_sign.items())), separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.md5(payload.encode("utf-8")).hexdigest().upper()
    return body.get("signature") == expected and body.get("language") == 0


def _transport(handler):
    return httpx.MockTransport(handler)


def _login_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "code": 0,
        "data": {"token": "tok-1", "tokenHeader": "Bearer ", "refreshToken": "ref-1"},
    })


# ---------------- client: login + signing ----------------

def test_login_stores_token_and_signs_requests():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        seen[path] = {"headers": dict(request.headers), "body": body}
        assert _signed_body_ok(body), f"bad signature on {path}"
        if path.endswith("/Login"):
            return _login_ok(request)
        if path.endswith("/TeamDayReport"):
            assert request.headers["Authorization"] == "Bearer tok-1"
            return httpx.Response(200, json={"code": 0, "data": {"list": []}})
        raise AssertionError(f"unexpected {path}")

    client = DmPlayClient("https://api.dmfirst9api.com", "https://www.dmfirst0.com",
                          client=httpx.Client(transport=_transport(handler)))
    out = client.login("9204133258", "secret")
    assert out["token"] == "tok-1"
    data = client.team_day_report("2026-11-05 00:00:00", user_id=2467206)
    assert data == {"list": []}
    assert seen["/api/webapi/Login"]["body"]["numberType"] == "919204133258"
    assert seen["/api/webapi/Login"]["body"]["pwd"] == "secret"
    assert seen["/api/webapi/Login"]["body"]["logintype"] == "mobile"
    assert seen["/api/webapi/TeamDayReport"]["body"]["userId"] == 2467206
    assert seen["/api/webapi/TeamDayReport"]["body"]["day"] == "2026-11-05 00:00:00"
    client.close()


def test_login_retries_with_plus_dial_code():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body.get("numberType"))
        if body.get("numberType") == "919204133258":
            return httpx.Response(200, json={"code": 1, "msg": "user not exist"})
        return _login_ok(request)

    client = DmPlayClient("https://api.dmfirst9api.com", "https://www.dmfirst0.com",
                          client=httpx.Client(transport=_transport(handler)))
    client.login("9204133258", "secret")
    assert calls == ["919204133258", "+919204133258"]
    client.close()


def test_team_day_report_without_list_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login"):
            return _login_ok(request)
        return httpx.Response(200, json={"code": 0, "data": {"something_else": 1}})

    client = DmPlayClient("https://api.dmfirst9api.com", "https://www.dmfirst0.com",
                          client=httpx.Client(transport=_transport(handler)))
    client.login("9204133258", "secret")
    with pytest.raises(AutoVerifyError):
        client.team_day_report("2026-11-05 00:00:00", user_id=2467206)
    client.close()


def test_stale_session_refreshes_once_then_retries():
    seen_auth = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Login"):
            return _login_ok(request)
        if path.endswith("/RefreshToken"):
            seen_auth.append(("refresh", request.headers.get("Authorization")))
            return httpx.Response(200, json={
                "code": 0, "data": {"token": "tok-2", "tokenHeader": "Bearer ",
                                    "refreshToken": "ref-2"},
            })
        if path.endswith("/TeamDayReport"):
            seen_auth.append(("report", request.headers.get("Authorization")))
            if len([a for a in seen_auth if a[0] == "report"]) == 1:
                return httpx.Response(200, json={"code": 11, "msg": "token expired"})
            return httpx.Response(200, json={"code": 0, "data": {"list": []}})
        raise AssertionError(path)

    client = DmPlayClient("https://api.dmfirst9api.com", "https://www.dmfirst0.com",
                          client=httpx.Client(transport=_transport(handler)))
    client.login("9204133258", "secret")
    data = client.team_day_report("2026-11-05 00:00:00", user_id=2467206)
    assert data == {"list": []}
    # The refresh call used the refresh token; the retry used the new token.
    assert ("refresh", "Bearer ref-1") in seen_auth
    assert ("report", "Bearer tok-2") in seen_auth
    client.close()


# ---------------- check_uid logic ----------------

class _FakeClient:
    """Minimal DmPlayClient stand-in: team_day_report by date string."""

    def __init__(self, by_day: dict, default=None):
        self.by_day = by_day
        self.default = default if default is not None else []
        self.calls = []

    def login(self, *a, **k):
        return {}

    def close(self):
        pass

    def team_day_report(self, day, user_id=None, level=0, page_no=1, page_size=10):
        self.calls.append((day, user_id))
        return {"list": self.by_day.get(day, self.default)}


def _rows(uid, deposit):
    return [{"userID": uid, "rechargeAmount": deposit, "commissionAmount": 0}]


def test_check_uid_passes_when_deposit_over_minimum():
    # Newest day: user present, deposit 0. Day before: deposit 500.
    fake = _FakeClient({
        "2026-11-05 00:00:00": _rows(2467206, 0),
        "2026-11-04 00:00:00": _rows(2467206, 500),
    })
    out = check_uid(fake, "2467206", lookback_days=14, min_deposit=300.0,
                    today=date(2026, 11, 6))
    assert out["passed"] is True and out["reason"] == "passed"
    assert out["found"] is True and out["deposit_total"] == 500.0
    # Early break: only the two days needed were queried.
    assert len(out["days_checked"]) == 2


def test_check_uid_uid_not_found():
    fake = _FakeClient({}, default=[])
    out = check_uid(fake, "9999999", lookback_days=3, today=date(2026, 11, 6))
    assert out["passed"] is False and out["reason"] == "uid_not_found"
    assert out["found"] is False and len(out["days_checked"]) == 3


def test_check_uid_deposit_below_minimum():
    fake = _FakeClient({
        "2026-11-05 00:00:00": _rows(2467206, 100),
        "2026-11-04 00:00:00": _rows(2467206, 150),
    })
    out = check_uid(fake, "2467206", lookback_days=3, min_deposit=300.0,
                    today=date(2026, 11, 6))
    assert out["passed"] is False and out["reason"] == "deposit_below_minimum"
    assert out["deposit_total"] == 250.0


def test_check_uid_non_numeric_never_calls_provider():
    fake = _FakeClient({})
    out = check_uid(fake, "ABC123", lookback_days=14, today=date(2026, 11, 6))
    assert out["passed"] is False and out["reason"] == "uid_not_numeric"
    assert fake.calls == []


def test_check_uid_provider_error_propagates():
    class _Boom(_FakeClient):
        def team_day_report(self, *a, **k):
            raise AutoVerifyError("boom")

    with pytest.raises(AutoVerifyError):
        check_uid(_Boom({}), "2467206", lookback_days=3, today=date(2026, 11, 6))


# ---------------- DB flow: run + batch ----------------

def _settings(**overrides):
    base = dict(
        autoverify_base_url="https://api.dmfirst9api.com",
        autoverify_origin="https://www.dmfirst0.com",
        autoverify_username="9204133258",
        autoverify_password="secret",
        autoverify_dial_code="91",
        autoverify_min_deposit=300.0,
        autoverify_lookback_days=14,
    )
    base.update(overrides)
    return Settings(**base)


def _make_factory():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    factory = get_session_factory(url)
    db = factory()
    user = User(telegram_id=777001, username="tester", first_name="Test")
    db.add(user)
    db.commit()
    db.close()
    return factory, user.id


def _add_submission(factory, user_id, uid, status="pending_review"):
    db = factory()
    row = Submission(user_id=user_id, registration_uid=uid, status=status)
    db.add(row)
    db.commit()
    sid = row.id
    db.close()
    return sid


def _get_row(factory, sid):
    db = factory()
    row = db.query(Submission).filter(Submission.id == sid).first()
    out = {
        "status": row.status,
        "review_message": row.review_message,
        "auto_check_result": json.loads(row.auto_check_result) if row.auto_check_result else None,
        "auto_checked_at": row.auto_checked_at,
    }
    db.close()
    return out


def test_run_auto_check_verifies_on_clean_pass():
    factory, user_id = _make_factory()
    sid = _add_submission(factory, user_id, "2467206")
    fake = _FakeClient({"2026-11-05 00:00:00": _rows(2467206, 500)})
    # Pin "today" so the date-keyed fake is deterministic.
    settings = _settings(autoverify_lookback_days=1)
    out = autoverify.run_auto_check_for_submission(
        factory, sid, settings, client=fake, today=date(2026, 11, 6))
    assert out["passed"] is True
    row = _get_row(factory, sid)
    assert row["status"] == "verified"
    assert "Automatic check se verify ho gaya" in (row["review_message"] or "")
    assert row["auto_check_result"]["passed"] is True
    assert row["auto_check_result"]["deposit_total"] == 500.0
    assert row["auto_checked_at"] is not None
    # History records the system actor, never a human admin.
    db = factory()
    from app.db import ReviewHistory
    hist = db.query(ReviewHistory).filter(ReviewHistory.submission_id == sid).all()
    db.close()
    assert len(hist) == 1
    assert hist[0].admin_telegram_id == 0
    assert hist[0].new_status == "verified"
    assert "Auto-verified by system" in (hist[0].internal_note or "")


def test_run_auto_check_leaves_pending_when_deposit_too_low():
    factory, user_id = _make_factory()
    sid = _add_submission(factory, user_id, "2467206")
    fake = _FakeClient({"2026-11-05 00:00:00": _rows(2467206, 0)})
    settings = _settings(autoverify_lookback_days=1)
    out = autoverify.run_auto_check_for_submission(
        factory, sid, settings, client=fake, today=date(2026, 11, 6))
    assert out["passed"] is False and out["reason"] == "deposit_below_minimum"
    row = _get_row(factory, sid)
    assert row["status"] == "pending_review"
    assert row["auto_check_result"]["reason"] == "deposit_below_minimum"


def test_run_auto_check_skipped_when_not_configured():
    factory, user_id = _make_factory()
    sid = _add_submission(factory, user_id, "2467206")
    settings = _settings(autoverify_username="", autoverify_password="")
    out = autoverify.run_auto_check_for_submission(factory, sid, settings, client=None)
    assert out["skipped"] is True and out["reason"] == "not_configured"
    assert _get_row(factory, sid)["status"] == "pending_review"


def test_run_auto_check_fails_closed_on_provider_error():
    factory, user_id = _make_factory()
    sid = _add_submission(factory, user_id, "2467206")

    class _Boom(_FakeClient):
        def team_day_report(self, *a, **k):
            raise AutoVerifyError("provider down")

    out = autoverify.run_auto_check_for_submission(
        factory, sid, _settings(autoverify_lookback_days=1), client=_Boom({}),
        today=date(2026, 11, 6))
    assert out["passed"] is False and out["reason"] == "provider_error"
    row = _get_row(factory, sid)
    assert row["status"] == "pending_review"
    assert row["auto_check_result"]["reason"] == "provider_error"


def test_batch_checks_pending_and_skips_recently_checked():
    factory, user_id = _make_factory()
    sid_pass = _add_submission(factory, user_id, "2467206")
    sid_fail = _add_submission(factory, user_id, "2467207")

    class _PerUid(_FakeClient):
        def team_day_report(self, day, user_id=None, **k):
            if user_id == 2467206:
                return {"list": _rows(2467206, 500)}
            return {"list": _rows(user_id, 0)}

    settings = _settings(autoverify_lookback_days=1)
    results = autoverify.check_pending_submissions(factory, settings, client=_PerUid({}))
    by_id = {r["submission_id"]: r for r in results}
    assert by_id[sid_pass]["passed"] is True
    assert by_id[sid_fail]["passed"] is False
    assert _get_row(factory, sid_pass)["status"] == "verified"
    assert _get_row(factory, sid_fail)["status"] == "pending_review"
    # Second run right away: the failing one was just checked, so nothing is due.
    results2 = autoverify.check_pending_submissions(factory, settings, client=_PerUid({}))
    assert results2 == []
