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


async def provider_check(registration_uid: str) -> dict:
    """Extension point for a real provider API / webhook (spec §6).

    Today there is no provider integration configured, so this honestly
    returns 'unavailable' and no caller treats it as verification.
    When a provider exists, implement the call here and record its
    evidence in ReviewHistory / AuditEvent — do not bypass admin review.
    """
    return {"provider_verified": None, "evidence": None, "reason": "no provider integration configured"}
