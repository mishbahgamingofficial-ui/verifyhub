"""Tests for the signal accuracy tracker (honest record, real data only).

Run:  cd verification-platform && python -m pytest tests/ -q

The live DmFirst provider is NEVER called here: the provider is a
fixed fixture (constant current period, controllable results) or a
failing provider for the best-effort settlement case. Periods use a
2027-01-01 date so they can never collide with the wall-clock-derived
fixture periods in test_prediction.py.

Ordering note: test_accuracy_summary_empty runs first and uses the
"30s" timer before any test in this suite records a 30s signal, so it
sees a genuinely empty record. Tests seed rows directly through a DB
session and use distinct timers/periods per case, so they do not
interfere with each other or with test_prediction.py.
"""
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from test_api import ADMIN_ID, H, client  # noqa: E402
from test_prediction import _user_with_status  # noqa: E402

import app.prediction as pred  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SignalRecord, get_session_factory  # noqa: E402
from app.prediction import ProviderError, classify, record_signal  # noqa: E402


def _db():
    return get_session_factory(get_settings().database_url)()


def _count(timer, period=None):
    db = _db()
    try:
        q = db.query(SignalRecord).filter(SignalRecord.timer == timer)
        if period is not None:
            q = q.filter(SignalRecord.period == period)
        return q.count()
    finally:
        db.close()


def _result(period, number):
    c = classify(number)
    return {"period": period, "number": number, "colour": c["colour"], "size": c["size"], "ts": None}


class _FixedProvider:
    """Deterministic provider: constant current period, fixed results."""

    def __init__(self, current_period, results):
        self._current = current_period
        self._results = list(results)

    def fetch_current(self, timer):
        now = datetime(2027, 1, 1, 12, 0, 0)
        return {
            "period": self._current,
            "start_time": now,
            "end_time": now + timedelta(seconds=60),
            "server_time": now,
        }

    def fetch_results(self, timer, limit=20):
        return list(self._results)[:limit]


class _FailingProvider:
    def fetch_current(self, timer):
        raise ProviderError("boom")

    def fetch_results(self, timer, limit=20):
        raise ProviderError("boom")


def _seed(timer, period, signal, reason="test"):
    db = _db()
    try:
        row = SignalRecord(timer=timer, period=period, signal=signal, reason=reason)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ---------------- summary with nothing settled yet ----------------

def test_accuracy_summary_empty(monkeypatch):
    # Must run before any 30s signal is recorded in this suite.
    monkeypatch.setattr(pred, "get_provider", lambda: _FixedProvider("20270101100010800", []))
    uid = 9300
    _user_with_status(uid, "verified")
    r = client.get("/api/prediction/accuracy?timer=30s", headers=H(uid))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timer"] == "30s"
    assert d["records"] == []
    assert d["summary"] == {
        "total": 0, "settled": 0, "correct": 0, "wrong": 0, "pending": 0,
        "accuracy_pct": None,  # null while nothing has settled
    }


# ---------------- recording is idempotent ----------------

def test_record_signal_is_idempotent():
    timer, period = "5m", "20270101100010800"
    db = _db()
    try:
        record_signal(db, timer, period, "BIG", "reason one")
        record_signal(db, timer, period, "SMALL", "reason two")  # must not overwrite
        record_signal(db, timer, "20270101100010801", "SMALL", "other period")
    finally:
        db.close()
    assert _count(timer, period) == 1
    db = _db()
    try:
        row = db.query(SignalRecord).filter(
            SignalRecord.timer == timer, SignalRecord.period == period
        ).first()
        assert row.signal == "BIG" and row.reason == "reason one"  # first one wins
        assert row.correct is None and row.actual_number is None  # still pending
    finally:
        db.close()


def test_prediction_records_signal_once(monkeypatch):
    monkeypatch.setattr(
        pred, "get_provider",
        lambda: _FixedProvider("20270101100010800", [_result("20270101100010799", 4)]),
    )
    uid = 9301
    _user_with_status(uid, "verified")
    for _ in range(2):  # same period both times: no duplicate
        r = client.get("/api/prediction?timer=1m", headers=H(uid))
        assert r.status_code == 200, r.text
    assert _count("1m", "20270101100010801") == 1


# ---------------- settlement + summary math ----------------

def test_accuracy_settles_and_summarizes(monkeypatch):
    results = [_result("20270101100010790", 7), _result("20270101100010791", 2)]
    monkeypatch.setattr(pred, "get_provider", lambda: _FixedProvider("20270101100010800", results))
    _seed("3m", "20270101100010790", "BIG")    # 7 is big -> correct
    _seed("3m", "20270101100010791", "BIG")    # 2 is small -> wrong
    _seed("3m", "20270101100010792", "SMALL")  # not in results -> stays pending
    uid = 9302
    _user_with_status(uid, "verified")
    r = client.get("/api/prediction/accuracy?timer=3m", headers=H(uid))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timer"] == "3m"
    assert [rec["period"] for rec in d["records"]] == [
        "20270101100010792", "20270101100010791", "20270101100010790",
    ]  # newest first
    by_period = {rec["period"]: rec for rec in d["records"]}
    assert by_period["20270101100010790"]["correct"] is True
    assert by_period["20270101100010790"]["actual_number"] == 7
    assert by_period["20270101100010790"]["actual_size"] == "BIG"
    assert by_period["20270101100010791"]["correct"] is False
    assert by_period["20270101100010791"]["actual_number"] == 2
    assert by_period["20270101100010791"]["actual_size"] == "SMALL"
    assert by_period["20270101100010792"]["correct"] is None  # still pending
    assert by_period["20270101100010792"]["actual_number"] is None
    assert d["summary"] == {
        "total": 3, "settled": 2, "correct": 1, "wrong": 1, "pending": 1,
        "accuracy_pct": 50.0,  # 1 / 2, never rounded up
    }


def test_accuracy_provider_failure_returns_records_as_is(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: _FailingProvider())
    _seed("1m", "20270101100010785", "BIG")
    uid = 9303
    _user_with_status(uid, "verified")
    r = client.get("/api/prediction/accuracy?timer=1m", headers=H(uid))
    assert r.status_code == 200, r.text  # settlement failure is not a 503
    rec = next(
        (x for x in r.json()["records"] if x["period"] == "20270101100010785"), None
    )
    assert rec is not None and rec["correct"] is None  # still pending


# ---------------- gating ----------------

@pytest.mark.parametrize("status", ["pending_review", "under_review", "rejected", "info_required"])
def test_accuracy_forbidden_for_non_verified(monkeypatch, status):
    monkeypatch.setattr(pred, "get_provider", lambda: _FixedProvider("20270101100010800", []))
    uid = {"pending_review": 9311, "under_review": 9312, "rejected": 9313, "info_required": 9314}[status]
    _user_with_status(uid, status)
    r = client.get("/api/prediction/accuracy", headers=H(uid))
    assert r.status_code == 403
    assert "unlocks after verification" in r.json()["detail"]


def test_accuracy_forbidden_when_not_submitted(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: _FixedProvider("20270101100010800", []))
    r = client.get("/api/prediction/accuracy", headers=H(9315))
    assert r.status_code == 403


def test_accuracy_requires_auth():
    assert client.get("/api/prediction/accuracy").status_code == 401


def test_accuracy_bad_timer_is_422(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: _FixedProvider("20270101100010800", []))
    uid = 9316
    _user_with_status(uid, "verified")
    assert client.get("/api/prediction/accuracy?timer=2m", headers=H(uid)).status_code == 422
    assert client.get("/api/prediction/accuracy?timer=abc", headers=H(uid)).status_code == 422
