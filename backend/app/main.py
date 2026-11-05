"""VerifyHub API — FastAPI app.

Routes:
  GET  /api/config            public, non-secret config (registration URL, UID rule, project name)
  GET  /api/me                authenticated user + is_admin (derived server-side)
  GET  /api/me/submissions    own submissions only (IDOR-safe: filtered by user_id)
  GET  /api/me/latest         own latest submission, or not_submitted
  POST /api/submissions       create, with duplicate + rate-limit protection
  GET  /api/admin/stats       admin only, derived from real DB rows
  GET  /api/admin/submissions admin only, search / filter / sort
  GET  /api/admin/submissions/{id}  admin only, full UID + history
  POST /api/admin/submissions/{id}/status  admin only, validated transition + audit + notify
  GET  /api/admin/audit       admin only, recent audit events

The frontend is served from ../frontend when present, so one process can
serve both API and Mini App in development. In production put the
frontend behind HTTPS (see README) and set MINI_APP_URL to that URL.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .auth import get_current_user, get_db_session, get_identity, require_admin
from .config import Settings, get_settings
from .db import AuditEvent, ReviewHistory, Submission, User, mask_uid
from .notifications import notify_user
from .rate_limit import RateLimitExceeded, limiter
from .security import TelegramIdentity
from .services import ACTIVE_STATUSES, STATUSES, can_transition

logger = logging.getLogger("verifyhub.api")
app = FastAPI(title="VerifyHub API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Mini App is loaded inside Telegram; auth is initData, not cookies.
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data", "Content-Type"],
)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _public_submission(s: Submission) -> dict:
    return {
        "id": s.id,
        "uid_masked": mask_uid(s.registration_uid),
        "reference": s.reference,
        "status": s.status,
        "review_message": s.review_message,
        "submitted_at": _iso(s.submitted_at),
        "updated_at": _iso(s.updated_at),
    }


def _admin_submission(s: Submission) -> dict:
    d = _public_submission(s)
    d.update(
        {
            "registration_uid": s.registration_uid,
            "user_telegram_id": s.user.telegram_id if s.user else None,
            "user_username": s.user.username if s.user else None,
            "user_first_name": s.user.first_name if s.user else None,
        }
    )
    return d


def _audit(db: Session, actor: int, action: str, record_type: str, record_id: int | None, meta: dict | None = None):
    db.add(
        AuditEvent(
            actor_telegram_id=actor,
            action=action,
            record_type=record_type,
            record_id=record_id,
            meta=json.dumps(meta or {}),
        )
    )


# ---------------- public config ----------------

@app.get("/api/config")
def public_config(settings: Settings = Depends(get_settings)):
    return {
        "project_name": settings.project_name,
        "registration_url": settings.registration_url,
        "uid_pattern": settings.uid_pattern,
        "uid_pattern_description": settings.uid_pattern_description,
        "has_registration_url": bool(settings.registration_url),
    }


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------- user routes ----------------

@app.get("/api/me")
def me(identity: TelegramIdentity = Depends(get_identity), user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)):
    return {
        "telegram_id": identity.telegram_id,
        "username": identity.username,
        "first_name": identity.first_name,
        "is_admin": settings.is_admin(identity.telegram_id),
    }


@app.get("/api/me/submissions")
def my_submissions(user: User = Depends(get_current_user), db: Session = Depends(get_db_session)):
    rows = db.query(Submission).filter(Submission.user_id == user.id).order_by(desc(Submission.submitted_at)).all()
    return [_public_submission(r) for r in rows]


@app.get("/api/me/latest")
def my_latest(user: User = Depends(get_current_user), db: Session = Depends(get_db_session)):
    row = db.query(Submission).filter(Submission.user_id == user.id).order_by(desc(Submission.submitted_at)).first()
    if row is None:
        return {"status": "not_submitted", "submission": None}
    return {"status": row.status, "submission": _public_submission(row)}


class SubmissionIn(BaseModel):
    registration_uid: str = Field(..., min_length=1, max_length=128)
    reference: str | None = Field(default=None, max_length=128)
    confirm_correct: bool = False

    @field_validator("registration_uid")
    @classmethod
    def strip_uid(cls, v: str) -> str:
        return v.strip()


@app.post("/api/submissions", status_code=201)
def create_submission(
    payload: SubmissionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    identity: TelegramIdentity = Depends(get_identity),
):
    try:
        limiter.check(f"submit:{identity.telegram_id}", settings.submission_per_hour, 3600)
        limiter.check(f"api:{identity.telegram_id}", settings.api_per_minute, 60)
    except RateLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many submissions. Please try again later.") from None
    if not payload.confirm_correct:
        raise HTTPException(status_code=422, detail="Please confirm that the UID is correct.")
    uid = payload.registration_uid
    if not settings.uid_regex.match(uid):
        raise HTTPException(status_code=422, detail=f"UID format is invalid. {settings.uid_pattern_description}")
    # Duplicate protection: one active submission per user (pending/under_review),
    # and the same UID cannot be re-submitted while it is active for this user.
    # UIDs are NOT assumed globally unique (spec §7).
    active = (
        db.query(Submission)
        .filter(Submission.user_id == user.id, Submission.status.in_(ACTIVE_STATUSES))
        .order_by(desc(Submission.submitted_at))
        .first()
    )
    if active is not None:
        if active.registration_uid == uid:
            raise HTTPException(status_code=409, detail="This UID is already under review for your account.")
        raise HTTPException(
            status_code=409,
            detail="You already have a submission under review. Wait for its outcome before submitting another.",
        )
    row = Submission(
        user_id=user.id,
        registration_uid=uid,
        reference=(payload.reference.strip() if payload.reference else None),
        status="pending_review",
    )
    db.add(row)
    db.flush()
    _audit(db, identity.telegram_id, "submission_created", "submission", row.id, {"status": "pending_review"})
    db.commit()
    db.refresh(row)
    return _public_submission(row)


# ---------------- admin routes ----------------

@app.get("/api/admin/stats")
def admin_stats(admin: TelegramIdentity = Depends(require_admin), db: Session = Depends(get_db_session)):
    counts = {s: 0 for s in STATUSES}
    for row in db.query(Submission.status).all():
        counts[row[0]] = counts.get(row[0], 0) + 1
    recent = (
        db.query(Submission).order_by(desc(Submission.submitted_at)).limit(10).all()
    )
    return {
        "total": sum(counts.values()),
        "by_status": counts,
        "recent": [_admin_submission(r) for r in recent],
    }


@app.get("/api/admin/submissions")
def admin_list(
    admin: TelegramIdentity = Depends(require_admin),
    db: Session = Depends(get_db_session),
    search: str = Query(default=""),
    status: str = Query(default=""),
    sort: str = Query(default="newest"),
):
    q = db.query(Submission).join(User)
    if status and status in STATUSES:
        q = q.filter(Submission.status == status)
    if search:
        term = search.strip()
        # Search by UID (exact-ish) or by submission id or telegram id.
        if term.isdigit():
            q = q.filter(
                (Submission.id == int(term))
                | (User.telegram_id == int(term))
                | (Submission.registration_uid.like(f"%{term}%"))
            )
        else:
            q = q.filter(Submission.registration_uid.like(f"%{term}%"))
    q = q.order_by(Submission.submitted_at if sort == "oldest" else desc(Submission.submitted_at))
    rows = q.limit(200).all()
    return [_admin_submission(r) for r in rows]


@app.get("/api/admin/submissions/{submission_id}")
def admin_detail(submission_id: int, admin: TelegramIdentity = Depends(require_admin), db: Session = Depends(get_db_session)):
    row = db.query(Submission).filter(Submission.id == submission_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    history = (
        db.query(ReviewHistory)
        .filter(ReviewHistory.submission_id == submission_id)
        .order_by(desc(ReviewHistory.created_at))
        .all()
    )
    return {
        "submission": _admin_submission(row),
        "history": [
            {
                "id": h.id,
                "admin_telegram_id": h.admin_telegram_id,
                "previous_status": h.previous_status,
                "new_status": h.new_status,
                "reason": h.reason,
                "internal_note": h.internal_note,
                "created_at": _iso(h.created_at),
            }
            for h in history
        ],
    }


class StatusChangeIn(BaseModel):
    to_status: str
    message: str | None = Field(default=None, max_length=1000)
    internal_note: str | None = Field(default=None, max_length=1000)


@app.post("/api/admin/submissions/{submission_id}/status")
async def admin_change_status(
    submission_id: int,
    payload: StatusChangeIn,
    background: BackgroundTasks,
    admin: TelegramIdentity = Depends(require_admin),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    if payload.to_status not in STATUSES:
        raise HTTPException(status_code=422, detail="Unknown status.")
    if payload.to_status in ("rejected", "info_required") and not (payload.message or "").strip():
        raise HTTPException(status_code=422, detail="A review message is required when rejecting or requesting more information.")
    row = db.query(Submission).filter(Submission.id == submission_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if not can_transition(row.status, payload.to_status):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot move a submission from {row.status} to {payload.to_status}.",
        )
    previous = row.status
    row.status = payload.to_status
    if payload.message is not None:
        row.review_message = payload.message.strip() or None
    row.updated_at = datetime.now(timezone.utc)
    db.add(
        ReviewHistory(
            submission_id=row.id,
            admin_telegram_id=admin.telegram_id,
            previous_status=previous,
            new_status=payload.to_status,
            reason=(payload.message.strip() if payload.message else None),
            internal_note=(payload.internal_note.strip() if payload.internal_note else None),
        )
    )
    _audit(
        db,
        admin.telegram_id,
        "status_changed",
        "submission",
        row.id,
        {"previous_status": previous, "new_status": payload.to_status},
    )
    db.commit()
    db.refresh(row)
    # Notify ONLY after commit (spec §5).
    owner = db.query(User).filter(User.id == row.user_id).first()
    if owner is not None:
        background.add_task(
            notify_user,
            settings.bot_token,
            owner.telegram_id,
            payload.to_status,
            row.review_message,
            settings.mini_app_url,
        )
    return _admin_submission(row)


@app.get("/api/admin/audit")
def admin_audit(admin: TelegramIdentity = Depends(require_admin), db: Session = Depends(get_db_session)):
    rows = db.query(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(100).all()
    return [
        {
            "id": r.id,
            "actor_telegram_id": r.actor_telegram_id,
            "action": r.action,
            "record_type": r.record_type,
            "record_id": r.record_id,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please try again later."})


# Serve the Mini App from the same process in dev (see module docstring).
_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
