"""Delete all non-admin users (and their submissions + review history).

Shared by the Render-shell script (backend/scripts/clean_users.py) and
the admin bot button ("Clean Users"). It is NEVER reachable over HTTP.

What it deletes: every row in `users` whose telegram id is not an admin
(env ADMIN_TELEGRAM_IDS plus the bot-added `admins` table), plus those
users' `submissions` and the submissions' `review_history` rows.

What it deliberately keeps: the admin user rows, the `admins` table,
`signal_records` (prediction accuracy history) and `audit_events`
(logs). Those are not users, and wiping them would erase the signal
record and the audit trail.

Safety: dry run (the default) only counts. The real run still returns
the same counts, so the caller can show them before confirming.
There is no undo.
"""
from __future__ import annotations

import json
import logging

from .db import Admin, AuditEvent, ReviewHistory, SignalRecord, Submission, User

logger = logging.getLogger("verifyhub.clean_users")


def collect_keep_ids(db, settings, extra_keep_ids: set[int] | None = None) -> set[int]:
    """Telegram ids that stay: env admins + bot-added admins (+ extras)."""
    keep: set[int] = set(settings.admin_telegram_ids)
    keep |= {row[0] for row in db.query(Admin.telegram_id).all()}
    if extra_keep_ids:
        keep |= set(extra_keep_ids)
    return keep


def clean(session_factory, settings, dry_run: bool = True,
          extra_keep_ids: set[int] | None = None) -> dict:
    """Delete non-admin users + their submissions/history. Returns counts.

    keep ids are collected from the settings + the admins table.
    Signal records and audit events are never touched. Dry run changes
    nothing.
    """
    db = session_factory()
    try:
        keep_ids = collect_keep_ids(db, settings, extra_keep_ids)
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
        logger.warning("Cleaned users: deleted %s users, %s submissions.",
                       len(drop), len(sub_ids))
        return summary
    finally:
        db.close()


def summary_json(summary: dict) -> str:
    return json.dumps(summary, indent=2)
