"""Backtest for the WinGo signal engine — analysis tool, NOT a pytest test.

Run:  cd verification-platform && .venv/bin/python tests/backtest_signal.py [--fetch]

- --fetch: pulls real DmFirst results (500 periods per timer, 1m + 30s)
  via DmFirstProvider paging and saves them to
  tests/backtest_data_1m.json / tests/backtest_data_30s.json.
  Without --fetch the saved sample is reused, so the backtest is
  reproducible without re-hitting the provider.
- The backtest then measures, for each strategy variant, how often the
  signal's size matched the actual next result — using ONLY results
  before each target period (no look-ahead). WinGo draws are
  independent, so ~50% is the expected baseline for every variant; a
  variant is only adopted if it beats 50% by more than ~2 standard
  errors on n >= 100 per timer without losing on the other timer.
  Nothing here touches the accuracy tracker: signal_records is a
  historical fact and is never rewritten by this script.

This file is deliberately NOT collected by pytest (its name matches
neither test_*.py nor *_test.py) and is never imported by the app.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

# The sandbox sets NO_PROXY with bracketed IPv6 entries that httpx
# cannot parse (same workaround as tests/test_api.py).
for _k in ("no_proxy", "NO_PROXY"):
    os.environ[_k] = "localhost,127.0.0.1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.prediction import (  # noqa: E402
    TIMER_TYPE_IDS,
    DmFirstProvider,
    classify,
    predict,
)

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_PERIODS = 500
PAGE_SIZE = 50
WARMUP = 50  # largest window tested; every target needs >=50 prior results


def _data_path(timer: str) -> str:
    return os.path.join(HERE, f"backtest_data_{timer}.json")


def fetch_sample(timer: str, target: int = TARGET_PERIODS) -> list[dict]:
    """Pull real results for one timer, newest first, via paged calls."""
    provider = DmFirstProvider()
    type_id = TIMER_TYPE_IDS[timer]
    seen: dict[str, dict] = {}
    page = 1
    while len(seen) < target and page <= 30:
        last_error: Exception | None = None
        data = None
        for attempt in range(3):  # outer retries on top of the provider's own 3x
            try:
                data = provider._post(
                    "GetNoaverageEmerdList",
                    {"typeId": type_id, "pageNo": page, "pageSize": PAGE_SIZE},
                )
                break
            except Exception as exc:  # noqa: BLE001 - best-effort fetch
                last_error = exc
                time.sleep(2 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"fetch failed for {timer} page {page}: {last_error}")
        items = data.get("list", []) if isinstance(data, dict) else []
        if not items:
            break
        for item in items:
            try:
                number = int(item["number"])
                c = classify(number)
            except (KeyError, ValueError, TypeError):
                continue
            period = str(item["issueNumber"])
            if period not in seen:
                seen[period] = {
                    "period": period,
                    "number": number,
                    "colour": str(item.get("colour") or c["colour"]).strip().lower() or c["colour"],
                    "size": c["size"],
                }
        page += 1
        time.sleep(0.3)  # polite pacing; never hammer the provider
    out = sorted(seen.values(), key=lambda r: r["period"], reverse=True)
    # Consecutiveness check: periods are ints and should decrement by 1.
    periods = [int(r["period"]) for r in out]
    gaps = sum(1 for a, b in zip(periods, periods[1:]) if a - b != 1)
    print(f"  {timer}: {len(out)} periods, {gaps} gaps, "
          f"newest {out[0]['period']} oldest {out[-1]['period']}")
    return out


def save_sample(timer: str, results: list[dict]) -> None:
    payload = {
        "timer": timer,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "DmFirst GetNoaverageEmerdList (paged, real results)",
        "results": results,  # newest first
    }
    with open(_data_path(timer), "w") as f:
        json.dump(payload, f)
    print(f"  saved {_data_path(timer)} ({len(results)} results)")


def load_sample(timer: str) -> list[dict]:
    with open(_data_path(timer)) as f:
        return json.load(f)["results"]


def _size_of(r: dict) -> str:
    return r.get("size") or classify(r["number"])["size"]


VARIANTS = {
    # All parameters are pinned explicitly: the shipped predict()
    # defaults changed after this backtest (to window 30 + recency),
    # so the baseline must stay the ORIGINAL logic (window 20, plain
    # counts, mean reversion) for the table to remain reproducible.
    "V0 baseline (w20, revert)": lambda h: predict(h, window=20, weighting="none"),
    "V1 window 30": lambda h: predict(h, window=30, weighting="none"),
    "V2 window 50": lambda h: predict(h, window=50, weighting="none"),
    "V3 recency w20": lambda h: predict(h, window=20, weighting="recency"),
    "V4 recency w30": lambda h: predict(h, window=30, weighting="recency"),
    "V5 recency w50": lambda h: predict(h, window=50, weighting="recency"),
    "V6 follow w20": lambda h: predict(h, window=20, weighting="none", direction="follow"),
    "V7 follow w30": lambda h: predict(h, window=30, weighting="none", direction="follow"),
    "V8 no-signal w20 (medium only)": lambda h: predict(h, window=20, weighting="none", min_strength=6),
    "V9 no-signal w30 (medium only)": lambda h: predict(h, window=30, weighting="none", min_strength=6),
    # Sanity checks: the newest ball's size, repeated and opposed.
    "V10 repeat last": lambda h: {"size": _size_of(h[0])} if h else None,
    "V11 anti last": lambda h: {"size": "small" if _size_of(h[0]) == "big" else "big"} if h else None,
}


def backtest(results_newest_first: list[dict]) -> dict[str, dict]:
    """No-look-ahead backtest. Returns {variant: metrics}.

    Chronological order is oldest -> newest. For target index i, the
    signal is generated from results[:i] only (reversed to newest
    first, as predict() expects), then compared with results[i].
    """
    chrono = list(reversed(results_newest_first))
    out: dict[str, dict] = {}
    for name, fn in VARIANTS.items():
        n = signalled = correct = 0
        for i in range(WARMUP, len(chrono)):
            history = list(reversed(chrono[:i]))  # newest first, before target
            signal = fn(history)
            n += 1
            if signal is None:
                continue
            signalled += 1
            if signal["size"] == _size_of(chrono[i]):
                correct += 1
        acc = correct / signalled if signalled else None
        # Standard error under the 50% null (the honest yardstick: is
        # this distinguishable from a coin flip?).
        se_null = math.sqrt(0.25 / signalled) if signalled else None
        z = (acc - 0.5) / se_null if acc is not None and se_null else None
        out[name] = {
            "n": n,
            "signalled": signalled,
            "coverage_pct": round(signalled / n * 100, 1) if n else None,
            "correct": correct,
            "accuracy_pct": round(acc * 100, 1) if acc is not None else None,
            "se_pct": round(se_null * 100, 1) if se_null is not None else None,
            "z_vs_50": round(z, 2) if z is not None else None,
        }
    return out


def _print_table(title: str, rows: dict[str, dict]) -> None:
    print(f"\n{title}")
    print(f"{'variant':<34} {'n':>4} {'sign':>5} {'cov%':>6} {'right':>5} {'acc%':>6} {'se%':>5} {'z':>6}")
    for name, m in rows.items():
        acc = f"{m['accuracy_pct']:.1f}" if m["accuracy_pct"] is not None else "—"
        se = f"{m['se_pct']:.1f}" if m["se_pct"] is not None else "—"
        z = f"{m['z_vs_50']:+.2f}" if m["z_vs_50"] is not None else "—"
        print(f"{name:<34} {m['n']:>4} {m['signalled']:>5} {m['coverage_pct']:>6} "
              f"{m['correct']:>5} {acc:>6} {se:>5} {z:>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true", help="fetch fresh real data first")
    args = ap.parse_args()
    if args.fetch:
        for timer in ("1m", "30s"):
            print(f"Fetching {timer} ...")
            save_sample(timer, fetch_sample(timer))
    all_rows: dict[str, dict[str, dict]] = {}
    for timer in ("1m", "30s"):
        results = load_sample(timer)
        print(f"\nBacktesting {timer}: {len(results)} periods in sample")
        rows = backtest(results)
        all_rows[timer] = rows
        _print_table(f"--- {timer} ---", rows)
    # Pooled across timers (same variants, independent draws).
    pooled: dict[str, dict] = {}
    for name in VARIANTS:
        n = sum(all_rows[t][name]["n"] for t in ("1m", "30s"))
        s = sum(all_rows[t][name]["signalled"] for t in ("1m", "30s"))
        c = sum(all_rows[t][name]["correct"] for t in ("1m", "30s"))
        acc = c / s if s else None
        se = math.sqrt(0.25 / s) if s else None
        pooled[name] = {
            "n": n, "signalled": s,
            "coverage_pct": round(s / n * 100, 1) if n else None,
            "correct": c,
            "accuracy_pct": round(acc * 100, 1) if acc is not None else None,
            "se_pct": round(se * 100, 1) if se is not None else None,
            "z_vs_50": round((acc - 0.5) / se, 2) if acc is not None and se else None,
        }
    _print_table("--- POOLED (1m + 30s) ---", pooled)
    print("\nReading the table: acc% is the size accuracy on signalled periods; "
          "z_vs_50 is how many standard errors the accuracy sits from 50% "
          "(|z| < 2 means indistinguishable from a coin flip). Coverage is "
          "the share of periods a variant actually signalled.")


if __name__ == "__main__":
    main()
