"""Tests for the WinGo prediction feature (verified users only).

Run:  cd verification-platform && python -m pytest tests/ -q

The live DmFirst provider is NEVER called here: every endpoint test
monkeypatches app.prediction.get_provider to a fixture (or a failing
provider for the 503 case). The provider tests below use recorded
payloads through httpx.MockTransport.
"""
import sys

import httpx
import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from test_api import ADMIN_ID, H, client  # noqa: E402

import app.prediction as pred  # noqa: E402
from app.prediction import (  # noqa: E402
    TIMER_SECONDS,
    DmFirstProvider,
    FixtureProvider,
    ProviderError,
    build_signed_body,
    classify,
    get_prediction,
    predict,
)


def _user_with_status(uid, target):
    """Submit a UID for uid, then drive it to target status via the admin API."""
    r = client.post("/api/submissions", headers=H(uid),
                    json={"registration_uid": f"PRD{uid}X1", "confirm_correct": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    if target == "pending_review":
        return sid
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID),
                    json={"to_status": "under_review"})
    assert r.status_code == 200, r.text
    if target == "under_review":
        return sid
    payload = {"to_status": target}
    if target in ("rejected", "info_required"):
        payload["message"] = "Reviewed."
    r = client.post(f"/api/admin/submissions/{sid}/status", headers=H(ADMIN_ID), json=payload)
    assert r.status_code == 200, r.text
    return sid


# ---------------- classify() — confirmed WinGo mapping ----------------

def test_classify_all_numbers():
    expected = {
        0: ("red,violet", "small"),
        1: ("green", "small"),
        2: ("red", "small"),
        3: ("green", "small"),
        4: ("red", "small"),
        5: ("green,violet", "big"),
        6: ("red", "big"),
        7: ("green", "big"),
        8: ("red", "big"),
        9: ("green", "big"),
    }
    for number, (colour, size) in expected.items():
        got = classify(number)
        assert got["colour"] == colour, number
        assert got["size"] == size, number
    with pytest.raises(ValueError):
        classify(10)
    with pytest.raises(ValueError):
        classify(-1)


# ---------------- predict() — deterministic and self-consistent ----------------

def _results(numbers):
    return [
        {"period": f"2026110510001{i:03d}", "number": n, **classify(n), "ts": None}
        for i, n in enumerate(numbers)
    ]


def test_predict_is_deterministic_and_self_consistent():
    hist = _results([3, 7, 2, 8, 4, 9, 1, 6, 2, 4, 7, 3, 8, 1, 9, 6, 4, 2, 7, 3])
    first = predict(hist)
    second = predict(hist)
    assert first == second  # same history -> same signal, no randomness
    assert first["confidence"] in ("low", "medium")  # never "high"
    assert isinstance(first["reason"], str) and first["reason"]
    # The representative ball must match the signal's size and colour.
    c = classify(first["number"])
    assert c["size"] == first["size"]
    assert first["colour"] in c["colour"].split(",")


def test_predict_empty_history_falls_back():
    s = predict([])
    assert s["confidence"] == "low"
    assert s["number"] in range(10)


def test_predict_violet_hit_is_named():
    # Newest ball is violet (5): the signal watches violet again.
    s = predict(_results([5, 3, 7, 2, 8, 4, 9, 1, 6, 2]))
    assert s["colour"] == "violet" and s["number"] == 5 and s["size"] == "big"
    assert "Violet" in s["reason"]


# ---------------- signing — deterministic given fixed random/timestamp ----------------

def test_signing_matches_known_vectors():
    rnd = "0123456789abcdef0123456789abcdef"
    body = build_signed_body({"typeId": 1}, random=rnd, timestamp=1700000000)
    assert body["signature"] == "71558D65C46E1DDFB84746EB62DA69A1"
    assert body["language"] == 0 and body["random"] == rnd
    assert body["timestamp"] == 1700000000 and body["typeId"] == 1
    # Empty params (GetTypeList).
    body2 = build_signed_body({}, random=rnd, timestamp=1700000000)
    assert body2["signature"] == "72530D55804E6C7423DF7B9177E9146E"
    # Same inputs -> same signature; a different random changes it.
    assert build_signed_body({"typeId": 1}, random=rnd, timestamp=1700000000)["signature"] == body["signature"]
    assert build_signed_body({"typeId": 1}, random="f" * 32, timestamp=1700000000)["signature"] != body["signature"]
    # Signature is uppercase hex.
    assert body["signature"] == body["signature"].upper()
    int(body["signature"], 16)


# ---------------- DmFirst provider — recorded payloads, no live calls ----------------

def _recorded_list_payload():
    return {"code": 0, "msg": "Succeed", "data": {"list": [
        {"issueNumber": "20261105100010782", "number": "4", "colour": "red", "premium": "4"},
        {"issueNumber": "20261105100010781", "number": "5", "colour": "green,violet", "premium": "5"},
        {"issueNumber": "20261105100010780", "number": "0", "colour": "red,violet", "premium": "0"},
    ]}}


def test_dmfirst_provider_maps_recorded_history():
    def handler(request):
        assert request.url.path.endswith("/GetNoaverageEmerdList")
        return httpx.Response(200, json=_recorded_list_payload())

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    results = provider.fetch_results("1m", limit=10)
    assert [r["number"] for r in results] == [4, 5, 0]  # newest first
    assert results[0] == {"period": "20261105100010782", "number": 4, "colour": "red", "size": "small", "ts": None}
    assert results[1]["colour"] == "green,violet" and results[1]["size"] == "big"
    assert results[2]["colour"] == "red,violet" and results[2]["size"] == "small"


def test_dmfirst_provider_maps_game_issue():
    payload = {"code": 0, "msg": "Succeed", "data": {
        "issueNumber": "20261105100010782", "startTime": "2026-11-05 18:31:00",
        "endTime": "2026-11-05 18:32:00", "serviceTime": "2026-11-05 18:31:35", "intervalM": 1.0,
    }}

    def handler(request):
        assert request.url.path.endswith("/GetGameIssue")
        return httpx.Response(200, json=payload)

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    cur = provider.fetch_current("1m")
    assert cur["period"] == "20261105100010782"
    # Countdown uses the SERVER clock: endTime - serviceTime = 25s.
    assert (cur["end_time"] - cur["server_time"]).total_seconds() == 25


def test_dmfirst_provider_retries_flaky_connection():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.RemoteProtocolError("IncompleteRead")
        return httpx.Response(200, json=_recorded_list_payload())

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    results = provider.fetch_results("1m", limit=10)
    assert len(results) == 3 and calls["n"] == 3


def test_dmfirst_provider_raises_after_repeated_failure():
    def handler(request):
        raise httpx.ConnectError("down")

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError):
        provider.fetch_results("1m")


def test_dmfirst_provider_rejects_error_code_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"code": 5, "msg": "bad signature"})

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError):
        provider.fetch_results("1m")
    assert calls["n"] == 1  # an error code is not retried


# ---------------- fixture provider — both timers ----------------

def test_fixture_provider_both_timers():
    fx = FixtureProvider()
    seen_periods = set()
    for timer in ("30s", "1m"):
        cur = fx.fetch_current(timer)
        res = fx.fetch_results(timer, limit=20)
        assert len(res) == 20
        # Results are completed periods only: newest is current - 1.
        assert int(res[0]["period"]) == int(cur["period"]) - 1
        assert int(cur["period"]) > int(res[1]["period"])
        remaining = (cur["end_time"] - cur["server_time"]).total_seconds()
        assert 0 <= remaining <= TIMER_SECONDS[timer]
        assert len(cur["period"]) == 17  # YYYYMMDD + 1000 + 5-digit sequence
        seen_periods.add(cur["period"])
    assert len(seen_periods) == 2  # the two timers run on different sequences


def test_get_prediction_with_fixture():
    d = get_prediction("1m", provider=FixtureProvider())
    assert d["timer"] == "1m"
    assert int(d["period"]) == int(d["current_period"]) + 1
    assert len(d["results"]) == 10
    assert d["signal"]["confidence"] in ("low", "medium")
    assert d["next_period_at"] and d["server_time"] and d["generated_at"]


# ---------------- endpoint: verified-only gating ----------------

@pytest.mark.parametrize("status", ["pending_review", "under_review", "rejected", "info_required"])
def test_prediction_forbidden_for_non_verified(monkeypatch, status):
    monkeypatch.setattr(pred, "get_provider", lambda: FixtureProvider())
    uid = {"pending_review": 9201, "under_review": 9202, "rejected": 9203, "info_required": 9204}[status]
    _user_with_status(uid, status)
    r = client.get("/api/prediction", headers=H(uid))
    assert r.status_code == 403
    assert "unlocks after verification" in r.json()["detail"]


def test_prediction_forbidden_when_not_submitted(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: FixtureProvider())
    r = client.get("/api/prediction", headers=H(9205))
    assert r.status_code == 403
    assert "unlocks after verification" in r.json()["detail"]


def test_prediction_requires_auth():
    assert client.get("/api/prediction").status_code == 401


def test_prediction_verified_user_gets_signal(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: FixtureProvider())
    uid = 9100
    _user_with_status(uid, "verified")
    for timer in ("1m", "30s"):
        r = client.get(f"/api/prediction?timer={timer}", headers=H(uid))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["timer"] == timer
        assert int(d["period"]) == int(d["current_period"]) + 1
        assert len(d["results"]) == 10
        assert d["signal"]["confidence"] in ("low", "medium")
        c = classify(d["signal"]["number"])
        assert c["size"] == d["signal"]["size"]
        assert d["signal"]["colour"] in c["colour"].split(",")


def test_prediction_bad_timer_is_422(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: FixtureProvider())
    uid = 9101
    _user_with_status(uid, "verified")
    assert client.get("/api/prediction?timer=2m", headers=H(uid)).status_code == 422
    assert client.get("/api/prediction?timer=abc", headers=H(uid)).status_code == 422


class _FailingProvider:
    def fetch_current(self, timer):
        raise ProviderError("boom")

    def fetch_results(self, timer, limit=20):
        raise ProviderError("boom")


def test_prediction_provider_failure_is_503(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: _FailingProvider())
    uid = 9102
    _user_with_status(uid, "verified")
    r = client.get("/api/prediction", headers=H(uid))
    assert r.status_code == 503
    assert "temporarily unavailable" in r.json()["detail"]
