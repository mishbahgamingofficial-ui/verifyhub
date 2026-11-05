"""Tiny in-memory sliding-window rate limiter.

Deliberately dependency-free and per-process. For multi-worker production
deployments, replace the store with Redis behind the same `check()` API —
the call sites in main.py do not need to change.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.time()
        bucket = self._buckets[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise RateLimitExceeded(f"Rate limit exceeded for {key}")
        bucket.append(now)


limiter = RateLimiter()
