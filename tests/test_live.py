"""Tests for the public live ticker endpoint GET /api/live.

Run:  cd verification-platform && python -m pytest tests/ -q

The endpoint serves real WinGo 1-min data from the provider — public,
no auth, no user data, nothing invented. Tests monkeypatch
app.prediction.get_provider to a fixture (or a failing provider for the
503 case), so the live DmFirst API is never called here.
"""
import sys

import httpx
import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from test_api import client  # noqa: E402

import app.prediction as pred  # noqa: E402
from app.prediction import FixtureProvider, ProviderError, clear_live_cache, get_live


@pytest.fixture(autouse=True)
def _fresh_live_cache():
    clear_live_cache()
    yield
    clear_live_cache()


class _FailingProvider:
    def fetch_current(self, timer):
        raise ProviderError("boom")

    def fetch_results(self, timer, limit=20):
        raise ProviderError("boom")


def test_live_is_public_and_shaped(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: FixtureProvider())
    r = client.get("/api/live")  # no auth header at all
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timer"] == "1m"
    assert isinstance(d["period"], str) and len(d["period"]) == 17
    assert isinstance(d["ends_in_seconds"], int)
    assert 0 <= d["ends_in_seconds"] <= 60  # WinGo 1-min period length
    assert len(d["last"]) == 5
    for row in d["last"]:
        assert set(row) == {"period", "number", "colour", "size"}
        assert row["number"] in range(10)
        assert row["colour"] in ("red", "green", "red,violet", "green,violet")
        assert row["size"] in ("big", "small")
    # Newest first: periods strictly decrease down the list.
    periods = [int(row["period"]) for row in d["last"]]
    assert periods == sorted(periods, reverse=True)


def test_live_provider_failure_is_503(monkeypatch):
    monkeypatch.setattr(pred, "get_provider", lambda: _FailingProvider())
    r = client.get("/api/live")
    assert r.status_code == 503
    assert "temporarily unavailable" in r.json()["detail"]


def test_live_countdown_uses_server_clock():
    """ends_in_seconds comes from endTime - serviceTime on the server clock."""
    payload_issue = {"code": 0, "msg": "Succeed", "data": {
        "issueNumber": "20261105100010782", "startTime": "2026-11-05 18:31:00",
        "endTime": "2026-11-05 18:32:00", "serviceTime": "2026-11-05 18:31:35", "intervalM": 1.0,
    }}
    payload_list = {"code": 0, "msg": "Succeed", "data": {"list": [
        {"issueNumber": "20261105100010781", "number": "7", "colour": "green", "premium": "7"},
        {"issueNumber": "20261105100010780", "number": "2", "colour": "red", "premium": "2"},
        {"issueNumber": "20261105100010779", "number": "0", "colour": "red,violet", "premium": "0"},
        {"issueNumber": "20261105100010778", "number": "9", "colour": "green", "premium": "9"},
        {"issueNumber": "20261105100010777", "number": "5", "colour": "green,violet", "premium": "5"},
    ]}}

    def handler(request):
        if request.url.path.endswith("/GetGameIssue"):
            return httpx.Response(200, json=payload_issue)
        return httpx.Response(200, json=payload_list)

    from app.prediction import DmFirstProvider

    provider = DmFirstProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    d = get_live(provider=provider)
    assert d["period"] == "20261105100010782"
    assert d["ends_in_seconds"] == 25  # 18:32:00 - 18:31:35
    assert [row["number"] for row in d["last"]] == [7, 2, 0, 9, 5]


class _CountingProvider(FixtureProvider):
    def __init__(self):
        self.current_calls = 0
        self.results_calls = 0

    def fetch_current(self, timer):
        self.current_calls += 1
        return super().fetch_current(timer)

    def fetch_results(self, timer, limit=20):
        self.results_calls += 1
        return super().fetch_results(timer, limit)


def test_live_cache_dedupes_fast_repeat_calls():
    provider = _CountingProvider()
    first = get_live(provider=provider)
    second = get_live(provider=provider)
    assert first == second
    assert provider.current_calls == 1  # second call served from the ~3s cache
    assert provider.results_calls == 1
    clear_live_cache()
    get_live(provider=provider)
    assert provider.current_calls == 2  # cache cleared -> provider hit again
