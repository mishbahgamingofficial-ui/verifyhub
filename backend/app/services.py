"""Business rules — kept separate from HTTP so the bot and API share them.

Status model (honest labelling, spec §6):
- A submission is a *claim* that the user registered.
- "verified" here means *administrator-reviewed and approved*, not
  "independently confirmed by the registration provider" — EXCEPT for
  the automatic check (``app.autoverify``), which verifies a
  submission only when the registration site's own Promotion data
  shows the UID exists AND a deposit over Rs 300. That is still not
  a provider guarantee: the check reads the agent panel's data, the
  deposit is summed over recent days, and any doubt leaves the
  submission in the admin queue. Automatic decisions are recorded in
  review history with the system actor (telegram id 0), never
  attributed to a human admin.
- A future provider integration plugs in at `provider_check()` without
  rewriting the submission flow: it would add evidence, not silently
  flip a status.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import load_only

from .db import Admin, AuditEvent, ReviewHistory, Submission, User

# Statuses a submission can be in. "not_submitted" is a virtual UI state
# (no submission row exists), never stored.
STATUSES = ["pending_review", "under_review", "verified", "rejected", "info_required", "withdrawn"]

ACTIVE_STATUSES = ["pending_review", "under_review"]

# Admin transitions that are valid. Terminal states have no outgoing edges.
# "withdrawn" is user-initiated only (the user withdraws their own pending
# submission from the status card / Mini App) — never an admin decision.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"under_review", "verified", "rejected", "info_required", "withdrawn"},
    "under_review": {"pending_review", "verified", "rejected", "info_required", "withdrawn"},
    "info_required": {"pending_review", "under_review", "verified", "rejected"},
    "verified": set(),
    "rejected": set(),
    "withdrawn": set(),
}


def can_transition(previous: str, new: str) -> bool:
    return new in ALLOWED_TRANSITIONS.get(previous, set())


def is_admin(db, telegram_id: int, settings) -> bool:
    """Admin = env allowlist OR the bot-added admins table.

    The one admin check for the whole system: the API gate (auth), /api/me
    and the bot all call this, so an admin added with /addadmin gets the
    same access as one set in ADMIN_TELEGRAM_IDS.
    """
    if settings.is_admin(telegram_id):
        return True
    return db.query(Admin).filter(Admin.telegram_id == telegram_id).first() is not None


def add_admin(db, *, telegram_id: int, added_by: int):
    """Add a bot admin. Returns (True, None) or (False, human-readable reason)."""
    if db.query(Admin).filter(Admin.telegram_id == telegram_id).first() is not None:
        return False, "That user is already an admin."
    db.add(Admin(telegram_id=telegram_id, added_by_telegram_id=added_by))
    db.commit()
    return True, None


def remove_admin(db, *, telegram_id: int):
    """Remove a bot-added admin. Returns (True, None) or (False, reason)."""
    row = db.query(Admin).filter(Admin.telegram_id == telegram_id).first()
    if row is None:
        return False, "That user is not an admin in the bot-added list."
    db.delete(row)
    db.commit()
    return True, None


def list_db_admins(db):
    """Bot-added admins, oldest first."""
    return db.query(Admin).order_by(Admin.created_at).all()


def change_submission_status(db, *, submission_id: int, to_status: str, message: str | None,
                             admin_telegram_id: int, internal_note: str | None = None):
    """The one status transition, shared by the HTTP API and the bot commands.

    Validates, moves the submission, records ReviewHistory + AuditEvent and
    commits. Returns ``(row, error, not_found)``: on success ``(row, None,
    False)``; on failure ``(None, human-readable reason, whether the
    submission did not exist)``. Nothing is written when it fails. The
    caller notifies the user only after this returns successfully.

    ``admin_telegram_id`` is the actor: an admin for review decisions, or
    the user themself for a withdraw (``to_status="withdrawn"``) — the
    history/audit rows record who actually moved it, and the inspector
    renders a withdraw as the user's own action, not an admin decision.
    """
    if to_status not in STATUSES:
        return None, "Unknown status.", False
    if to_status in ("rejected", "info_required") and not (message or "").strip():
        return None, "A review message is required when rejecting or requesting more information.", False
    row = db.query(Submission).filter(Submission.id == submission_id).first()
    if row is None:
        return None, "Submission not found.", True
    if not can_transition(row.status, to_status):
        return None, f"Cannot move a submission from {row.status} to {to_status}.", False
    previous = row.status
    row.status = to_status
    if message is not None:
        row.review_message = message.strip() or None
    row.updated_at = datetime.now(timezone.utc)
    db.add(
        ReviewHistory(
            submission_id=row.id,
            admin_telegram_id=admin_telegram_id,
            previous_status=previous,
            new_status=to_status,
            reason=(message.strip() if message else None),
            internal_note=(internal_note.strip() if internal_note else None),
        )
    )
    db.add(
        AuditEvent(
            actor_telegram_id=admin_telegram_id,
            action="status_changed",
            record_type="submission",
            record_id=row.id,
            meta=json.dumps({"previous_status": previous, "new_status": to_status}),
        )
    )
    db.commit()
    return row, None, False


def user_badges(db, telegram_id: int, *, is_admin_user: bool) -> list[str]:
    """Badges earned from real data only — nothing invented or bought.

    👑 Admin — platform administrator.
    ✅ Verified — submission approved by an admin.
    ⚡ First Try — verified on the very first submission.
    ⭐ Early Bird — among the first 10 users ever.
    🏆 OG Member — among the first 50 users ever.
    📅 Veteran — account older than 30 days.
    🔁 Comeback — submitted more than once.

    The single source of truth for badges: the bot's profile card and
    the Mini App's /api/me both call this, so the two never disagree.
    """
    badges: list[str] = []
    if is_admin_user:
        badges.append("👑 Admin")
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user is None:
        return badges
    submissions = (
        db.query(Submission)
        .options(load_only(Submission.status, Submission.submitted_at))
        .filter(Submission.user_id == user.id)
        .order_by(Submission.submitted_at.asc())
        .all()
    )
    is_ver = any(s.status == "verified" for s in submissions)
    if is_ver:
        badges.append("✅ Verified")
        if len(submissions) == 1 and submissions[0].status == "verified":
            badges.append("⚡ First Try")
    rank = db.query(User).filter(User.id < user.id).count()
    if rank < 10:
        badges.append("⭐ Early Bird")
    elif rank < 50:
        badges.append("🏆 OG Member")
    first_seen = user.first_seen_at
    if first_seen is not None:
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - first_seen).days >= 30:
            badges.append("📅 Veteran")
    if len(submissions) >= 2:
        badges.append("🔁 Comeback")
    return badges


def vip_tier(badges: list[str], verified: bool, total_submissions: int) -> str:
    """Compute a user's real-data tier from their verification status and badges."""
    if "👑 Admin" in badges:
        return "👑 Sovereign Admin"
    if verified and ("⭐ Early Bird" in badges or "⚡ First Try" in badges or len(badges) >= 3):
        return "💎 Diamond VIP"
    if verified:
        return "🥇 Gold Verified"
    if total_submissions > 0:
        return "🥈 Silver Applicant"
    return "🌱 New Member"


async def provider_check(registration_uid: str) -> dict:
    """Extension point for a real provider API / webhook (spec §6).

    The live automatic check is ``app.autoverify`` (Promotion data:
    UID exists + deposit over Rs 300); this function stays as the
    generic hook and honestly reports that *it* has no provider
    configured. No caller treats this as verification.
    """
    return {"provider_verified": None, "evidence": None, "reason": "no provider integration configured"}
