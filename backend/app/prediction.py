"""WinGo colour-trading prediction — provider + deterministic signal engine.

The prediction page unlocks only after an administrator marks a user
Verified (the /api/prediction endpoint enforces that server-side).
This module only fetches recent results and computes a *pattern signal*:
it is honest labelling, not a forecast — the signal is never a
guarantee, never claims accuracy, and its confidence is never "high".

Providers
---------
- ``DmFirstProvider`` — the live provider. It calls the DmFirst results
  API (https://api.dmfirst9api.com, prefix /api/webapi) with the
  plain-MD5 request signing that API requires. No API key exists.
  Endpoints used (all public, no login): GetGameIssue (current period
  + server clock) and GetNoaverageEmerdList (recent results, newest
  first). The connection is flaky, so every call retries up to 3 times
  with backoff; a partial body counts only if it parses as complete
  JSON. A tiny server-side cache (5s) keeps rapid refreshes from
  hammering the provider.
- ``FixtureProvider`` — realistic fixture data, for tests and local
  development only. The live endpoint never uses it.

The API's issueNumber is OPAQUE (YYYYMMDD + 1000 + running sequence):
it is always read from GetGameIssue, never computed. Consecutive
periods increment by 1, so the *next* period id is current + 1.

WinGo rules (confirmed against the live API):
- Green = 1, 3, 7, 9; Red = 2, 4, 6, 8; 0 = red+violet; 5 = green+violet.
- 0-4 = Small, 5-9 = Big. The API carries no Big/Small field; it is
  derived from the number.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("verifyhub.prediction")

# Timers the API supports. The UI exposes 30s and 1m; 3m/5m are accepted
# by the endpoint for later use.
TIMER_TYPE_IDS = {"30s": 30, "1m": 1, "3m": 2, "5m": 3}
TIMER_SECONDS = {"30s": 30, "1m": 60, "3m": 180, "5m": 300}

DMFIRST_BASE_URL = "https://api.dmfirst9api.com"
DMFIRST_API_PREFIX = "/api/webapi"
# Cloudflare returns 403 (error 1010) for non-browser User-Agents, so the
# provider must identify as a browser. This is a generic UA string, not
# a credential.
DMFIRST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
PROVIDER_TIMEOUT_SECONDS = 8.0
PROVIDER_MAX_ATTEMPTS = 3
CACHE_TTL_SECONDS = 5.0


class ProviderError(Exception):
    """The results provider failed (network, bad payload, error code)."""


def classify(number: int) -> dict:
    """WinGo classification for one ball (0-9).

    Returns {"colour": ..., "size": ...}. Colour follows the API's own
    vocabulary: "red", "green", "red,violet" (0), "green,violet" (5).
    """
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 9:
        raise ValueError("number must be an int 0-9")
    if number == 0:
        colour = "red,violet"
    elif number == 5:
        colour = "green,violet"
    elif number % 2 == 0:
        colour = "red"
    else:
        colour = "green"
    return {"colour": colour, "size": "small" if number <= 4 else "big"}


def _base_colour(colour: str) -> str:
    """Base colour family: "red,violet" -> "red", "green" -> "green"."""
    return (colour or "").split(",")[0].strip().lower()


# Representative balls for a (colour, size) signal. Deterministic pick
# from these lists, so the same history always yields the same number.
_CANDIDATES: dict[tuple[str, str], list[int]] = {
    ("red", "small"): [2, 4],
    ("red", "big"): [6, 8],
    ("green", "small"): [1, 3],
    ("green", "big"): [7, 9],
    ("violet", "small"): [0],
    ("violet", "big"): [5],
}


def predict(results: list[dict]) -> dict:
    """Deterministic pattern signal from recent results (newest first).

    Analyses the last 20 results: colour streaks, Big/Small balance,
    and whether a violet ball just hit. The result is a *signal* —
    a named pattern, not a forecast. Confidence is "low" or "medium",
    never "high". No randomness and no clock: the same history always
    returns the same signal.
    """
    if not results:
        return {
            "size": "small",
            "colour": "red",
            "number": 2,
            "confidence": "low",
            "reason": "Not enough history yet — first signal.",
        }
    window = results[:20]
    newest = window[0]
    newest_base = _base_colour(newest.get("colour", ""))
    newest_size = newest.get("size") or classify(newest["number"])["size"]

    # Violet just hit on the newest ball: watch the violet family again.
    if newest["number"] in (0, 5):
        size = "small" if newest["number"] == 0 else "big"
        return {
            "size": size,
            "colour": "violet",
            "number": newest["number"],
            "confidence": "low",
            "reason": f"Violet just hit ({newest['number']}) — watching it again.",
        }

    # Streak of the newest base colour, counting from the top.
    streak = 0
    for r in window:
        if _base_colour(r.get("colour", "")) == newest_base:
            streak += 1
        else:
            break

    def _size(r: dict) -> str:
        return r.get("size") or classify(r["number"])["size"]

    big = sum(1 for r in window if _size(r) == "big")
    small = len(window) - big
    reds = sum(1 for r in window if _base_colour(r.get("colour", "")) == "red")
    greens = sum(1 for r in window if _base_colour(r.get("colour", "")) == "green")

    # Mean reversion: predict the minority side of each balance. Ties go
    # to the opposite of the newest result (deterministic).
    if big > small:
        size = "small"
    elif small > big:
        size = "big"
    else:
        size = "big" if newest_size == "small" else "small"
    if reds > greens:
        colour = "green"
    elif greens > reds:
        colour = "red"
    else:
        colour = "green" if newest_base == "red" else "red"

    parts = []
    if streak >= 2:
        parts.append(f"{streak} {newest_base}s in a row")
    parts.append(f"{'Big' if big > small else 'Small'} leading {big}-{small}")
    reason = ", ".join(parts)

    confidence = "medium" if (streak >= 3 or abs(big - small) >= 6) else "low"

    candidates = _CANDIDATES[(colour, size)]
    index = sum(r["number"] for r in window[:5]) % len(candidates)
    return {
        "size": size,
        "colour": colour,
        "number": candidates[index],
        "confidence": confidence,
        "reason": reason,
    }


# ---------------- request signing (DmFirst) ----------------

_SIGN_EXCLUDED_KEYS = {"signature", "track", "xosoBettingData"}


def build_signed_body(params: dict, random: str | None = None, timestamp: int | None = None) -> dict:
    """Build a signed DmFirst request body.

    Start with the endpoint params, add language: 0 and a 32-char hex
    random. The signature is UPPERCASE(MD5(compact JSON of those params
    SORTED by key, dropping null/"" values and the keys signature/track/
    xosoBettingData; values of 0 are kept)). The sent body is the params
    plus language, random, signature and a Unix-seconds timestamp (the
    timestamp itself is not part of the signature).
    """
    rnd = random if random is not None else uuid.uuid4().hex
    ts = timestamp if timestamp is not None else int(time.time())
    to_sign = {**params, "language": 0, "random": rnd}
    cleaned = {}
    for key in sorted(to_sign):
        value = to_sign[key]
        if value is None or value == "":
            continue
        if key in _SIGN_EXCLUDED_KEYS:
            continue
        cleaned[key] = value
    payload = json.dumps(cleaned, separators=(",", ":"), ensure_ascii=False)
    signature = hashlib.md5(payload.encode("utf-8")).hexdigest().upper()
    return {**params, "language": 0, "random": rnd, "signature": signature, "timestamp": ts}


def _parse_server_time(value: str) -> datetime:
    # The API returns naive "YYYY-MM-DD HH:MM:SS" strings. They are kept
    # naive on purpose: the countdown is computed from the *difference*
    # between endTime and serviceTime, which is timezone-independent.
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------- providers ----------------

class DmFirstProvider:
    """Live results provider: DmFirst public results API.

    ``client`` is injectable (httpx.Client, e.g. with a MockTransport)
    for tests; otherwise a short-lived client is built per call.
    """

    def __init__(self, base_url: str = DMFIRST_BASE_URL, client: httpx.Client | None = None,
                 cache_ttl: float = CACHE_TTL_SECONDS):
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple[str, str], tuple[float, object]] = {}

    def _cached(self, timer: str, kind: str):
        entry = self._cache.get((timer, kind))
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    def _store(self, timer: str, kind: str, data) -> None:
        self._cache[(timer, kind)] = (time.monotonic() + self._cache_ttl, data)

    def _post(self, endpoint: str, params: dict) -> dict:
        url = f"{self.base_url}{DMFIRST_API_PREFIX}/{endpoint}"
        body = build_signed_body(params)
        own_client = self._client is None
        client = self._client or httpx.Client(
            timeout=PROVIDER_TIMEOUT_SECONDS,
            headers={"User-Agent": DMFIRST_USER_AGENT, "Content-Type": "application/json"},
        )
        last_error: Exception | None = None
        try:
            for attempt in range(PROVIDER_MAX_ATTEMPTS):
                try:
                    resp = client.post(url, json=body)
                    # A truncated body raises here (or in .json() below);
                    # only a body that parses as complete JSON is usable.
                    data = resp.json()
                    if not isinstance(data, dict) or data.get("code") != 0:
                        code = data.get("code") if isinstance(data, dict) else "unknown"
                        msg = data.get("msg") if isinstance(data, dict) else ""
                        # A well-formed error response (e.g. bad signature)
                        # will not heal on retry — fail at once.
                        raise ProviderError(f"DmFirst {endpoint} returned code {code}: {msg}")
                    return data.get("data") or {}
                except ProviderError:
                    raise
                except (httpx.TransportError, ValueError) as exc:
                    # TransportError covers IncompleteRead / disconnects;
                    # ValueError covers a body that is not valid JSON.
                    last_error = exc
                    logger.warning("DmFirst %s attempt %s failed: %s", endpoint, attempt + 1, exc)
                    if attempt < PROVIDER_MAX_ATTEMPTS - 1:
                        time.sleep(0.2 * (attempt + 1))
        finally:
            if own_client:
                client.close()
        raise ProviderError(
            f"DmFirst {endpoint} failed after {PROVIDER_MAX_ATTEMPTS} attempts: {last_error}"
        )

    def fetch_current(self, timer: str) -> dict:
        """Current period from GetGameIssue: {period, start_time, end_time, server_time}."""
        cached = self._cached(timer, "current")
        if cached is not None:
            return cached
        data = self._post("GetGameIssue", {"typeId": TIMER_TYPE_IDS[timer]})
        try:
            out = {
                "period": str(data["issueNumber"]),
                "start_time": _parse_server_time(data["startTime"]),
                "end_time": _parse_server_time(data["endTime"]),
                "server_time": _parse_server_time(data["serviceTime"]),
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(f"GetGameIssue payload unexpected: {exc}") from exc
        self._store(timer, "current", out)
        return out

    def fetch_results(self, timer: str, limit: int = 20) -> list[dict]:
        """Recent results, newest first. Each is {period, number, colour, size, ts}."""
        cached = self._cached(timer, "results")
        if cached is not None:
            return list(cached)[:limit]
        data = self._post(
            "GetNoaverageEmerdList",
            {"typeId": TIMER_TYPE_IDS[timer], "pageNo": 1, "pageSize": max(limit, 10)},
        )
        items = data.get("list", []) if isinstance(data, dict) else []
        out: list[dict] = []
        for item in items:
            try:
                number = int(item["number"])
                c = classify(number)
            except (KeyError, ValueError, TypeError):
                continue
            colour = str(item.get("colour") or c["colour"]).strip().lower() or c["colour"]
            out.append(
                {
                    "period": str(item["issueNumber"]),
                    "number": number,
                    "colour": colour,
                    "size": c["size"],
                    "ts": None,  # the history endpoint carries no per-result time
                }
            )
        self._store(timer, "results", out)
        return out[:limit]


# Fixture numbers: a fixed, mixed sequence (includes 0 and 5) so the
# fixture signal varies but is fully deterministic.
_FIXTURE_NUMBERS = [3, 7, 2, 8, 0, 4, 9, 1, 6, 5, 2, 3, 8, 4, 7, 1, 9, 0, 6, 2, 5, 3, 8, 4, 7]
_FIXTURE_EPOCH = datetime(2026, 11, 1, tzinfo=timezone.utc)


class FixtureProvider:
    """Realistic fixture data — tests and local dev only. Never used live."""

    def _period_start(self, timer: str, when: datetime | None = None) -> datetime:
        interval = TIMER_SECONDS[timer]
        now = when or datetime.now(timezone.utc)
        epoch_seconds = int(now.timestamp())
        start_epoch = epoch_seconds - (epoch_seconds % interval)
        return datetime.fromtimestamp(start_epoch, tz=timezone.utc)

    def _period_id(self, timer: str, start: datetime) -> str:
        interval = TIMER_SECONDS[timer]
        elapsed = int((start - _FIXTURE_EPOCH).total_seconds()) // interval
        return f"{start.strftime('%Y%m%d')}1000{elapsed:05d}"

    def fetch_current(self, timer: str) -> dict:
        interval = TIMER_SECONDS[timer]
        start = self._period_start(timer)
        return {
            "period": self._period_id(timer, start),
            "start_time": start,
            "end_time": start + timedelta(seconds=interval),
            "server_time": datetime.now(timezone.utc),
        }

    def fetch_results(self, timer: str, limit: int = 20) -> list[dict]:
        interval = TIMER_SECONDS[timer]
        start = self._period_start(timer)
        out: list[dict] = []
        # Results are for completed periods only: the newest result is the
        # period just before the current one.
        for i in range(limit):
            period_start = start - timedelta(seconds=interval * (i + 1))
            number = _FIXTURE_NUMBERS[i % len(_FIXTURE_NUMBERS)]
            c = classify(number)
            out.append(
                {
                    "period": self._period_id(timer, period_start),
                    "number": number,
                    "colour": c["colour"],
                    "size": c["size"],
                    "ts": period_start.isoformat(),
                }
            )
        return out


_provider: DmFirstProvider | None = None


def get_provider() -> DmFirstProvider:
    """Live provider (cached). Tests monkeypatch this to inject a fixture."""
    global _provider
    if _provider is None:
        _provider = DmFirstProvider()
    return _provider


def get_prediction(timer: str, provider=None) -> dict:
    """Full prediction payload for one timer.

    ``period`` is the NEXT period id (current + 1) — the one the signal
    is for. ``next_period_at`` is when the current period closes, and
    ``server_time`` is the provider's clock at fetch time, so the
    frontend can run the countdown without trusting the client clock.
    """
    if timer not in TIMER_TYPE_IDS:
        raise ValueError(f"Unknown timer: {timer!r}")
    p = provider or get_provider()
    current = p.fetch_current(timer)
    history = p.fetch_results(timer, limit=20)
    if not history:
        raise ProviderError("No results returned by the provider.")
    signal = predict(history)
    try:
        next_period = str(int(current["period"]) + 1)
    except (ValueError, TypeError):
        next_period = current["period"]
    return {
        "timer": timer,
        "period": next_period,
        "current_period": current["period"],
        "next_period_at": _iso(current["end_time"]),
        "server_time": _iso(current["server_time"]),
        "results": history[:10],
        "signal": signal,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------- live ticker (public, real data only) ----------------

_LIVE_CACHE_TTL_SECONDS = 3.0
# provider identity -> (monotonic timestamp, payload). Many users poll
# the ticker every few seconds; this keeps them from hammering the
# provider on top of its own 5s cache.
_live_cache: dict[int, tuple[float, dict]] = {}


def clear_live_cache() -> None:
    """Drop the live-ticker cache (tests use this between cases)."""
    _live_cache.clear()


def get_live(provider=None) -> dict:
    """Live ticker payload for WinGo 1-min: current period + last 5 results.

    Public data only — no user, no auth, no invented activity. The
    countdown comes from the SERVER clock (endTime - serviceTime), so a
    wrong phone clock cannot skew it. Returns {"timer", "period",
    "ends_in_seconds", "last": [{period, number, colour, size}]}.
    """
    p = provider or get_provider()
    entry = _live_cache.get(id(p))
    if entry and time.monotonic() - entry[0] < _LIVE_CACHE_TTL_SECONDS:
        return entry[1]
    current = p.fetch_current("1m")
    history = p.fetch_results("1m", limit=5)
    if not history:
        raise ProviderError("No results returned by the provider.")
    ends = (current["end_time"] - current["server_time"]).total_seconds()
    out = {
        "timer": "1m",
        "period": current["period"],
        "ends_in_seconds": max(0, int(ends)),
        "last": [
            {
                "period": r["period"],
                "number": r["number"],
                "colour": r["colour"],
                "size": r["size"],
            }
            for r in history[:5]
        ],
    }
    _live_cache[id(p)] = (time.monotonic(), out)
    return out
