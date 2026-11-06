"""Delete all non-admin users (and their submissions + review history).

Admin utility — run it from the Render service shell, where DATABASE_URL
is already in the environment. It is NEVER reachable over HTTP.

What it deletes: every row in `users` whose telegram id is not an admin
(env ADMIN_TELEGRAM_IDS plus the bot-added `admins` table), plus those
users' `submissions` and the submissions' `review_history` rows.

What it deliberately keeps: the admin user rows, the `admins` table,
`signal_records` (prediction accuracy history) and `audit_events`
(logs). Those are not users, and wiping them would erase the signal
record and the audit trail.

Safety: without --confirm it only prints the counts it WOULD delete
(dry run). With --confirm it deletes and prints the counts it DID
delete. There is no undo — the user asked for a clean database
before launch, and the counts are shown first so a surprising
number can stop the run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# The script lives in backend/scripts/, the app in backend/app/ — make
# the app importable no matter which directory the shell starts in.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Admin, AuditEvent, ReviewHistory, SignalRecord, Submission, User, get_session_factory  # noqa: E402


def _admin_ids(env_value: str) -> set[int]:
    out: set[int] = set()
    for part in env_value.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def clean(session_factory, keep_ids: set[int], dry_run: bool = True) -> dict:
    """Delete non-admin users + their submissions/history. Returns counts.

    keep_ids are telegram ids that stay. Signal records and audit
    events are never touched. Dry run changes nothing.
    """
    db = session_factory()
    try:
        users = db.query(User).all()
        keep = [u for u in users if u.telegram_id in keep_ids]
        drop = [u for u in users if u.telegram_id not in keep_ids]
        drop_user_ids = [u.id for u in drop]
        sub_ids = [
            row[0]
            for row in db.query(Submission.id)
            .filter(Submission.user_id.in_(drop_user_ids))
            .all()
        ] if drop_user_ids else []
        hist_count = (
            db.query(ReviewHistory).filter(ReviewHistory.submission_id.in_(sub_ids)).count()
            if sub_ids else 0
        )
        summary = {
            "dry_run": dry_run,
            "users_total": len(users),
            "users_kept": len(keep),
            "kept_telegram_ids": sorted(u.telegram_id for u in keep),
            "users_deleted": len(drop),
            "submissions_deleted": len(sub_ids),
            "history_deleted": hist_count,
            "signal_records_kept": db.query(SignalRecord).count(),
            "audit_events_kept": db.query(AuditEvent).count(),
        }
        if dry_run:
            return summary
        if sub_ids:
            db.query(ReviewHistory).filter(ReviewHistory.submission_id.in_(sub_ids)).delete(
                synchronize_session=False
            )
            db.query(Submission).filter(Submission.id.in_(sub_ids)).delete(
                synchronize_session=False
            )
        if drop_user_ids:
            db.query(User).filter(User.id.in_(drop_user_ids)).delete(
                synchronize_session=False
            )
        db.commit()
        summary["dry_run"] = False
        return summary
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all non-admin users.")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete. Without this, only counts are printed.")
    parser.add_argument("--keep", default="",
                        help="Extra telegram ids to keep, comma-separated.")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    keep_ids = _admin_ids(os.environ.get("ADMIN_TELEGRAM_IDS", ""))
    keep_ids |= _admin_ids(args.keep)
    factory = get_session_factory(database_url)
    # Bot-added admins are admins too — their user rows stay.
    db = factory()
    try:
        keep_ids |= {row[0] for row in db.query(Admin.telegram_id).all()}
    finally:
        db.close()
    summary = clean(factory, keep_ids, dry_run=not args.confirm)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
