"""Delete all non-admin users — thin CLI wrapper around app.clean_users.

Run it from the Render service shell, where DATABASE_URL is already in
the environment. It is NEVER reachable over HTTP. See app.clean_users
for what is deleted and what is kept.

Usage:
    python scripts/clean_users.py            # dry run: only prints counts
    python scripts/clean_users.py --confirm  # actually deletes
    python scripts/clean_users.py --confirm --keep 123456789
"""
from __future__ import annotations

import argparse
import os
import sys

# The script lives in backend/scripts/, the app in backend/app/ — make
# the app importable no matter which directory the shell starts in.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clean_users import clean, summary_json  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402


def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all non-admin users.")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete. Without this, only counts are printed.")
    parser.add_argument("--keep", default="",
                        help="Extra telegram ids to keep, comma-separated.")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    factory = get_session_factory(settings.database_url)
    summary = clean(factory, settings, dry_run=not args.confirm,
                    extra_keep_ids=_ids(args.keep))
    print(summary_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
