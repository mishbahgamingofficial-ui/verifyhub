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

import copy
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


def _size_of(r: dict) -> str:
    return r.get("size") or classify(r["number"])["size"]


# Reason shown when the pattern is too weak to signal (see predict()'s
# min_strength): the panel stays visible with the countdown and history,
# it just carries no direction for that period.
NO_SIGNAL_REASON = "No clear pattern — wait for the next period."


def predict(results: list[dict], *, window: int = 30, weighting: str = "recency",
            direction: str = "revert", min_strength: int = 0) -> dict | None:
    """Deterministic pattern signal from recent results (newest first).

    Analyses the last ``window`` results: colour streaks, Big/Small
    balance, and whether a violet ball just hit. The result is a
    *signal* — a named pattern, not a forecast. Confidence is "low"
    or "medium", never "high". No randomness and no clock: the same
    history always returns the same signal.

    Defaults (window 30, recency weighting) are the most
    stable/defensible variant, chosen by backtest, not by an accuracy
    claim: tests/backtest_signal.py measured 12 variants on ~950 real
    periods (1m + 30s) and every one landed within ~1 standard error
    of 50% — WinGo draws are independent, so no pattern rule beats a
    coin flip. A 30-result window smooths single-ball noise versus 20,
    and recency weighting (0.85**i) keeps the pattern responsive to
    the latest balls. The accuracy tracker keeps reporting the real
    numbers, whatever they are.

    Parameters (all keyword-only; the backtest in
    tests/backtest_signal.py varies them to measure which variant, if
    any, beats chance):
    - window: how many recent results form the pattern window.
    - weighting: "none" (plain counts) or "recency" (exponential decay
      0.85**i, newest first, scaled back to window-sized counts so the
      reason still reads like "Big leading 11-9").
    - direction: "revert" (predict the minority side of each balance,
      the original mean-reversion logic) or "follow" (predict the
      majority side; ties follow the newest result instead of opposing
      it).
    - min_strength: when > 0, only signal when the pattern is strong:
      strength = max(abs(big - small), 2 * streak), i.e. the current
      "medium" condition (streak >= 3 or imbalance >= 6). Otherwise
      returns None (no signal for that period) instead of a weak one.
    """
    if not results:
        return {
            "size": "small",
            "colour": "red",
            "number": 2,
            "confidence": "low",
            "reason": "Not enough history yet — first signal.",
        }
    window_results = results[:window]
    newest = window_results[0]
    newest_base = _base_colour(newest.get("colour", ""))
    newest_size = newest.get("size") or classify(newest["number"])["size"]

    # Streak of the newest base colour, counting from the top.
    streak = 0
    for r in window_results:
        if _base_colour(r.get("colour", "")) == newest_base:
            streak += 1
        else:
            break

    if weighting == "recency":
        weights = [0.85 ** i for i in range(len(window_results))]
        total_w = sum(weights)
        scale = len(window_results) / total_w if total_w else 1.0
        big = sum(w for w, r in zip(weights, window_results) if _size_of(r) == "big") * scale
        small = sum(w for w, r in zip(weights, window_results) if _size_of(r) == "small") * scale
        reds = sum(w for w, r in zip(weights, window_results)
                   if _base_colour(r.get("colour", "")) == "red") * scale
        greens = sum(w for w, r in zip(weights, window_results)
                     if _base_colour(r.get("colour", "")) == "green") * scale
        weighted = True
    elif weighting != "none":
        raise ValueError(f"Unknown weighting: {weighting!r}")
    else:
        big = sum(1 for r in window_results if _size_of(r) == "big")
        small = len(window_results) - big
        reds = sum(1 for r in window_results if _base_colour(r.get("colour", "")) == "red")
        greens = sum(1 for r in window_results if _base_colour(r.get("colour", "")) == "green")
        weighted = False

    # Strength gate: only a strong pattern earns a signal. The score is
    # exactly the current "medium" condition (streak >= 3 or imbalance
    # >= 6), so min_strength=6 means "signal only on medium-or-better".
    if min_strength:
        strength = max(abs(big - small), 2 * streak)
        if strength < min_strength:
            return None

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

    def _fmt(n) -> str:
        return f"{n:.1f}" if weighted else str(int(n))

    if direction == "follow":
        # Streak-follow: predict the majority side of each balance.
        # Ties follow the newest result.
        size = "big" if big > small else "small" if small > big else newest_size
        colour = "red" if reds > greens else "green" if greens > reds else newest_base
    elif direction != "revert":
        raise ValueError(f"Unknown direction: {direction!r}")
    else:
        # Mean reversion: predict the minority side of each balance.
        # Ties go to the opposite of the newest result (deterministic).
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
    parts.append(
        f"{'Big' if big > small else 'Small'} leading {_fmt(big)}-{_fmt(small)}"
        + (" (recency-weighted)" if weighted else "")
    )
    reason = ", ".join(parts)

    confidence = "medium" if (streak >= 3 or abs(big - small) >= 6) else "low"

    candidates = _CANDIDATES[(colour, size)]
    index = sum(r["number"] for r in window_results[:5]) % len(candidates)
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


# One atomic snapshot per (provider, timer): the current period, the
# recent results and the signal derived from them are fetched once and
# then served identically to every user for a few seconds. Without
# this, the current-period read and the results read have independent
# 5s caches, so two users opening the page a second apart at a period
# boundary could see different histories/signals for the same timer.
# The snapshot stays live (it expires in seconds), it is never served
# on a provider failure, and it is keyed by provider identity so tests
# with fresh providers never share a snapshot. Callers always get a
# copy, so nobody can mutate the cached payload.
_PREDICTION_SNAPSHOT_TTL_SECONDS = 5.0
_prediction_snapshots: dict[tuple[int, str], tuple[float, dict]] = {}


def clear_prediction_cache() -> None:
    """Drop the prediction snapshot cache (tests use this between cases)."""
    _prediction_snapshots.clear()


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
    key = (id(p), timer)
    entry = _prediction_snapshots.get(key)
    if entry and entry[0] > time.monotonic():
        # Served from cache: tell the frontend how old this snapshot
        # is, so its countdown can subtract the age. Without this the
        # countdown ran up to 5s late at a period boundary — the
        # snapshot's server_time is from build time, but the frontend
        # anchored it at render time.
        served = copy.deepcopy(entry[1])
        served["snapshot_age_ms"] = int((time.monotonic() - entry[2]) * 1000)
        return served
    current = p.fetch_current(timer)
    # Fetch the full pattern window (30) for predict(); the frontend
    # still only shows the last 10 (history[:10] below).
    history = p.fetch_results(timer, limit=30)
    if not history:
        raise ProviderError("No results returned by the provider.")
    signal = predict(history)
    try:
        next_period = str(int(current["period"]) + 1)
    except (ValueError, TypeError):
        next_period = current["period"]
    out = {
        "timer": timer,
        "period": next_period,
        "current_period": current["period"],
        "next_period_at": _iso(current["end_time"]),
        "server_time": _iso(current["server_time"]),
        "results": history[:10],
        "signal": signal,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _prediction_snapshots[key] = (time.monotonic() + _PREDICTION_SNAPSHOT_TTL_SECONDS, out, time.monotonic())
    served = copy.deepcopy(out)
    served["snapshot_age_ms"] = 0
    return served


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


# ---------------- signal accuracy tracker (honest record, real data only) ----------------
# A signal is recorded when /api/prediction generates one, and settled
# (lazily, on read) once the period's real result is published. Nothing
# here invents, rounds up, or cherry-picks: the summary counts exactly
# what is stored, and accuracy_pct is null until a signal has settled.

from sqlalchemy import desc as _desc  # noqa: E402
from sqlalchemy.orm import Session as _Session  # noqa: E402

from .db import SignalRecord as _SignalRecord  # noqa: E402

_ACCURACY_LIMIT = 20


def record_signal(db: _Session, timer: str, period: str, size: str, reason: str | None,
                  confidence: str | None = None) -> None:
    """Record a generated signal, first one wins. Never raises.

    The (timer, period) unique constraint makes this idempotent: a
    repeat call for the same period leaves the original row untouched,
    so the record cannot be rewritten after the result is known.
    Recording must never break the prediction response, so every
    failure is logged and swallowed. A period with no signal is never
    recorded at all (callers skip the call when the signal is None) —
    the record is a record of signals, not of periods.
    """
    try:
        size = (size or "").strip().upper()
        if size not in ("BIG", "SMALL"):
            return
        conf = (confidence or "").strip().lower()
        if conf not in ("low", "medium"):
            conf = None
        exists = (
            db.query(_SignalRecord)
            .filter(_SignalRecord.timer == timer, _SignalRecord.period == str(period))
            .first()
        )
        if exists is not None:
            return
        db.add(
            _SignalRecord(
                timer=timer,
                period=str(period),
                signal=size,
                reason=(reason or "")[:500] or None,
                confidence=conf,
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Failed to record signal for %s %s; continuing.", timer, period)


def settle_signals(db: _Session, provider, timer: str) -> int:
    """Settle pending records for one timer against real results.

    Fetches the recent results and fills actual_number / actual_size /
    correct for every unsettled record whose period now appears in them.
    Best-effort: a provider failure leaves the records pending and is
    logged, never raised — the endpoint still returns the record as-is.
    Returns how many records were settled.
    """
    try:
        results = provider.fetch_results(timer, limit=20)
    except Exception:
        logger.warning("Accuracy settle skipped for %s: provider unavailable.", timer)
        return 0
    by_period = {r["period"]: r for r in results}
    if not by_period:
        return 0
    pending = (
        db.query(_SignalRecord)
        .filter(_SignalRecord.timer == timer, _SignalRecord.correct.is_(None))
        .all()
    )
    settled = 0
    try:
        for row in pending:
            result = by_period.get(row.period)
            if result is None:
                continue
            actual_size = classify(result["number"])["size"].upper()
            row.actual_number = int(result["number"])
            row.actual_size = actual_size
            row.correct = row.signal == actual_size
            settled += 1
        if settled:
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Failed to settle signals for %s; continuing.", timer)
        return 0
    return settled


def _serialize_record(row: _SignalRecord) -> dict:
    return {
        "period": row.period,
        "signal": row.signal,
        "reason": row.reason,
        "confidence": row.confidence,
        "actual_number": row.actual_number,
        "actual_size": row.actual_size,
        "correct": row.correct,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _confidence_bucket(row: _SignalRecord) -> str:
    """Confidence bucket for a record. Rows recorded before confidence
    tracking (confidence NULL) count as "low" — the conservative
    default, documented in the SignalRecord model — rather than
    inventing a confidence after the fact."""
    return row.confidence if row.confidence in ("low", "medium") else "low"


def get_accuracy(db: _Session, timer: str, provider=None, limit: int = _ACCURACY_LIMIT) -> dict:
    """Settle (best-effort) then return the last `limit` records, newest first.

    Summary counts exactly what is stored: total, settled, correct,
    wrong, pending, and accuracy_pct = correct / settled * 100 rounded
    to 1 decimal — or null while nothing has settled yet. The
    by_confidence split applies the same honest counting per
    confidence tier (NULL-confidence rows count as "low"). The provider
    is resolved here (module-level get_provider) so tests can
    monkeypatch it; callers pass one explicitly only to override.
    """
    p = provider or get_provider()
    settle_signals(db, p, timer)
    rows = (
        db.query(_SignalRecord)
        .filter(_SignalRecord.timer == timer)
        .order_by(_desc(_SignalRecord.id))
        .limit(limit)
        .all()
    )
    settled_rows = [r for r in rows if r.correct is not None]
    correct = sum(1 for r in settled_rows if r.correct)
    accuracy_pct = round(correct / len(settled_rows) * 100, 1) if settled_rows else None

    def _split(bucket: str) -> dict:
        settled_b = [r for r in settled_rows if _confidence_bucket(r) == bucket]
        correct_b = sum(1 for r in settled_b if r.correct)
        return {
            "settled": len(settled_b),
            "correct": correct_b,
            "accuracy_pct": round(correct_b / len(settled_b) * 100, 1) if settled_b else None,
        }

    return {
        "timer": timer,
        "records": [_serialize_record(r) for r in rows],
        "summary": {
            "total": len(rows),
            "settled": len(settled_rows),
            "correct": correct,
            "wrong": len(settled_rows) - correct,
            "pending": len(rows) - len(settled_rows),
            "accuracy_pct": accuracy_pct,
            "by_confidence": {"low": _split("low"), "medium": _split("medium")},
        },
    }
