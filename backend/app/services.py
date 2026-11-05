"""Business rules — kept separate from HTTP so the bot and API share them.

Status model (honest labelling, spec §6):
- A submission is a *claim* that the user registered.
- "verified" here means *administrator-reviewed and approved*, not
  "independently confirmed by the registration provider".
- A future provider integration plugs in at `provider_check()` without
  rewriting the submission flow: it would add evidence, not silently
  flip a status.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import AuditEvent, ReviewHistory, Submission

# Statuses a submission can be in. "not_submitted" is a virtual UI state
# (no submission row exists), never stored.
STATUSES = ["pending_review", "under_review", "verified", "rejected", "info_required"]

ACTIVE_STATUSES = ["pending_review", "under_review"]

# Admin transitions that are valid. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"under_review", "verified", "rejected", "info_required"},
    "under_review": {"pending_review", "verified", "rejected", "info_required"},
    "info_required": {"pending_review", "under_review", "verified", "rejected"},
    "verified": set(),
    "rejected": set(),
}


def can_transition(previous: str, new: str) -> bool:
    return new in ALLOWED_TRANSITIONS.get(previous, set())


def change_submission_status(db, *, submission_id: int, to_status: str, message: str | None,
                             admin_telegram_id: int, internal_note: str | None = None):
    """The one admin transition, shared by the HTTP API and the bot commands.

    Validates, moves the submission, records ReviewHistory + AuditEvent and
    commits. Returns ``(row, error, not_found)``: on success ``(row, None,
    False)``; on failure ``(None, human-readable reason, whether the
    submission did not exist)``. Nothing is written when it fails. The
    caller notifies the user only after this returns successfully.
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


async def provider_check(registration_uid: str) -> dict:
    """Extension point for a real provider API / webhook (spec §6).

    Today there is no provider integration configured, so this honestly
    returns 'unavailable' and no caller treats it as verification.
    When a provider exists, implement the call here and record its
    evidence in ReviewHistory / AuditEvent — do not bypass admin review.
    """
    return {"provider_verified": None, "evidence": None, "reason": "no provider integration configured"}
