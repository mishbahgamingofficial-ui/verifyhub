"""VerifyHub API — FastAPI app.

Routes:
  GET  /api/config            public, non-secret config (registration URL, UID rule, project name)
  GET  /api/me                authenticated user + is_admin (derived server-side)
  GET  /api/me/submissions    own submissions only (IDOR-safe: filtered by user_id)
  GET  /api/me/latest         own latest submission, or not_submitted
  GET  /api/prediction        verified users only (server-gated)
  GET  /api/live              public WinGo 1-min ticker (no auth, real data only)
  POST /api/submissions       create, with duplicate + rate-limit protection
  GET  /api/admin/stats       admin only, derived from real DB rows
  GET  /api/admin/submissions admin only, search / filter / sort
  GET  /api/admin/submissions/{id}  admin only, full UID + history
  POST /api/admin/submissions/{id}/status  admin only, validated transition + audit + notify
  GET  /api/admin/audit       admin only, recent audit events
  POST /telegram/webhook      Telegram bot updates (webhook mode, free tier)

The Telegram bot runs inside this process via webhook — no polling
worker is needed on the free tier. The same handlers as the polling
bot (app.bot_handlers) process the updates; see the webhook section
below. Startup never fails if the webhook cannot be registered.

The frontend is served from ../frontend when present, so one process can
serve both API and Mini App in development. In production put the
frontend behind HTTPS (see README) and set MINI_APP_URL to that URL.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telegram import Update as TelegramUpdate
from telegram.ext import Application as TelegramApplication

from .auth import get_current_user, get_db_session, get_identity, require_admin
from .config import Settings, get_settings
from .db import AuditEvent, ReviewHistory, Submission, User, get_session_factory, mask_uid
from .notifications import notify_admins, notify_user
from .prediction import TIMER_TYPE_IDS, ProviderError, get_accuracy, get_live, get_prediction, record_signal
from .rate_limit import RateLimitExceeded, limiter
from .security import TelegramIdentity
from .services import ACTIVE_STATUSES, STATUSES, change_submission_status

logger = logging.getLogger("verifyhub.api")


# ---------------- Telegram webhook (free tier: bot runs inside the API) ----------------
# The bot is NOT polled here. Telegram delivers updates to
# POST /telegram/webhook, which feeds them to a shared python-telegram-bot
# Application (built with updater=None) using the exact same handlers as
# the polling worker (app.bot_handlers). Only ONE transport may be active
# for a token at a time — never run the polling worker while this webhook
# is registered, or Telegram returns 409 conflicts.

_telegram_app: TelegramApplication | None = None


def get_telegram_app() -> TelegramApplication | None:
    """Build (once) the PTB Application used for webhook updates.

    Returns None when no BOT_TOKEN is configured. Built lazily so importing
    this module never touches the network.
    """
    global _telegram_app
    if _telegram_app is not None:
        return _telegram_app
    settings = get_settings()
    if not settings.bot_token:
        return None
    from .bot_handlers import build_telegram_application

    _telegram_app = build_telegram_application(settings.bot_token, with_updater=False)
    return _telegram_app


async def _initialize_telegram_app(ptb: TelegramApplication) -> bool:
    """Initialize the PTB app (idempotent). False on failure — never raises."""
    try:
        await ptb.initialize()
        return True
    except Exception:
        logger.exception("Failed to initialize Telegram application.")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Ensure the tables exist before serving traffic, with a short retry
    # so a briefly-unreachable DB right after a deploy does not 500 the
    # first real user. Failure here never crashes startup — the first
    # request retries table creation via get_db_session.
    for attempt in range(5):
        try:
            get_session_factory(settings.database_url)
            break
        except Exception:
            logger.exception("Database init failed (attempt %s/5).", attempt + 1)
            await asyncio.sleep(2 * (attempt + 1))
    try:
        ptb = get_telegram_app()
    except Exception:
        # Building the PTB app must never crash startup — the API keeps
        # serving even if the bot cannot be built.
        logger.exception("Failed to build Telegram application; webhook disabled.")
        ptb = None
    if ptb is not None:
        if await _initialize_telegram_app(ptb):
            if settings.public_base_url:
                if not settings.telegram_webhook_secret:
                    # Fail closed: registering a webhook with no secret
                    # would let anyone inject fake updates, so skip it
                    # until a secret is configured.
                    logger.warning(
                        "TELEGRAM_WEBHOOK_SECRET is not set; skipping webhook registration."
                    )
                else:
                    url = settings.public_base_url.rstrip("/") + "/telegram/webhook"
                    try:
                        await ptb.bot.set_webhook(url=url, secret_token=settings.telegram_webhook_secret)
                        info = await ptb.bot.get_webhook_info()
                        logger.info(
                            "Telegram webhook set: url=%s pending_update_count=%s",
                            info.url,
                            info.pending_update_count,
                        )
                    except Exception:
                        # Startup must never crash because the webhook could
                        # not be registered — the API keeps serving.
                        logger.exception("Failed to set Telegram webhook; API continues without it.")
    yield
    if ptb is not None:
        try:
            await ptb.shutdown()
        except Exception:
            logger.exception("Error shutting down Telegram application.")


app = FastAPI(title="VerifyHub API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Mini App is loaded inside Telegram; auth is initData, not cookies.
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data", "Content-Type"],
)


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    # Any database failure (stale connection, DB down, pool timeout) is a
    # 503 with a retryable message — never a bare 500. The request's
    # session is discarded by get_db_session, so no partial write persists.
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please try again in a moment."},
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
        "has_deposit_screenshot": bool(s.deposit_screenshot),
        "submitted_at": _iso(s.submitted_at),
        "updated_at": _iso(s.updated_at),
    }


def _admin_submission(s: Submission) -> dict:
    d = _public_submission(s)
    d.update(
        {
            "registration_uid": s.registration_uid,
            "deposit_screenshot": s.deposit_screenshot,
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
def health(settings: Settings = Depends(get_settings)):
    """Liveness + database reachability. A dead database is a 503 here so
    monitoring (and the cron pinger) can see it instead of the Mini App
    failing silently on the first real request."""
    try:
        factory = get_session_factory(settings.database_url)
        db = factory()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception:
        logger.exception("Health check: database unavailable.")
        return JSONResponse(status_code=503, content={"ok": False, "db": "unavailable"})
    return {"ok": True, "db": "ok"}


# ---------------- Telegram webhook ----------------

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, settings: Settings = Depends(get_settings)):
    """Receive Telegram updates via webhook (free tier: no polling worker).

    Telegram sends the X-Telegram-Bot-Api-Secret-Token header when a secret
    was registered with setWebhook. When TELEGRAM_WEBHOOK_SECRET is set we
    require an exact match (403 otherwise); when it is unset (local dev
    only) we log a warning and still accept the update.
    """
    secret = settings.telegram_webhook_secret
    if secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not token or not hmac.compare_digest(token, secret):
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")
    elif settings.bot_token:
        # A live bot with no secret would accept forged updates — fail
        # closed instead. (No token configured at all keeps the old
        # ignore-with-200 behaviour for local dev.)
        logger.warning("TELEGRAM_WEBHOOK_SECRET is not set; refusing webhook update.")
        raise HTTPException(status_code=503, detail="Webhook secret is not configured.")
    else:
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET is not set; accepting webhook update without secret check (local dev only)."
        )
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body.") from None
    try:
        ptb = get_telegram_app()
    except Exception:
        logger.exception("Failed to build Telegram application.")
        raise HTTPException(status_code=503, detail="Telegram application unavailable; please retry.") from None
    if ptb is None:
        # No BOT_TOKEN configured — nothing can process this update.
        logger.warning("Telegram webhook received but BOT_TOKEN is not set; update ignored.")
        return {"ok": True}
    if not await _initialize_telegram_app(ptb):
        # Transient failure (e.g. Telegram unreachable) — 503 so Telegram
        # retries delivery instead of dropping the update.
        raise HTTPException(status_code=503, detail="Telegram application unavailable; please retry.") from None
    try:
        update = TelegramUpdate.de_json(data, ptb.bot)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid Telegram update.") from None
    try:
        await ptb.process_update(update)
    except Exception:
        logger.exception("Failed to process Telegram update.")
        raise HTTPException(status_code=503, detail="Failed to process update; please retry.") from None
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


@app.get("/api/prediction")
def prediction(
    timer: str = Query(default="1m"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """WinGo prediction for verified users only.

    The gate is server-side: the user's latest submission must be
    status == "verified". The tab being visible in the Mini App means
    nothing — this endpoint is the lock. The signal is a pattern
    signal, never a guarantee (see app.prediction).
    """
    if timer not in TIMER_TYPE_IDS:
        raise HTTPException(status_code=422, detail="Unknown timer. Use 30s, 1m, 3m or 5m.")
    latest = (
        db.query(Submission)
        .filter(Submission.user_id == user.id)
        .order_by(desc(Submission.submitted_at))
        .first()
    )
    if latest is None or latest.status != "verified":
        raise HTTPException(status_code=403, detail="Prediction unlocks after verification.")
    try:
        d = get_prediction(timer)
    except ProviderError:
        raise HTTPException(status_code=503, detail="Results temporarily unavailable.") from None
    # Record the signal for the accuracy tracker (first one wins per
    # period). Best-effort by design: record_signal never raises, so a
    # recording failure can never break this response. A period with no
    # signal (predict() returned None under a strength threshold) is
    # not recorded — the record is a record of signals, not periods.
    if d["signal"] is not None:
        record_signal(db, timer, d["period"], d["signal"]["size"],
                      d["signal"]["reason"], d["signal"]["confidence"])
    return d


@app.get("/api/prediction/accuracy")
def prediction_accuracy(
    timer: str = Query(default="1m"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Signal accuracy record for verified users only — same gate as
    /api/prediction. Settles pending signals against real results
    (best-effort; a provider failure returns the records as-is) and
    returns the last 20 signals with an honest summary. Nothing here
    is estimated or rounded up: the counts are exactly what is stored.
    """
    if timer not in TIMER_TYPE_IDS:
        raise HTTPException(status_code=422, detail="Unknown timer. Use 30s, 1m, 3m or 5m.")
    latest = (
        db.query(Submission)
        .filter(Submission.user_id == user.id)
        .order_by(desc(Submission.submitted_at))
        .first()
    )
    if latest is None or latest.status != "verified":
        raise HTTPException(status_code=403, detail="Prediction unlocks after verification.")
    return get_accuracy(db, timer)


@app.get("/api/live")
def live():
    """Public live ticker: current WinGo 1-min period + last 5 results.

    No auth — it serves public results only, never any user data and
    never anything invented. The frontend polls it for the ticker bar
    and the bottom dock.
    """
    try:
        return get_live()
    except ProviderError:
        raise HTTPException(status_code=503, detail="Results temporarily unavailable.") from None


class SubmissionIn(BaseModel):
    registration_uid: str = Field(..., min_length=1, max_length=128)
    reference: str | None = Field(default=None, max_length=128)
    confirm_correct: bool = False
    # Deposit proof screenshot from step 2 of the flow: a data URL
    # (client-side compressed JPEG). Capped so one upload cannot bloat
    # the free database; the client compresses to well under this.
    deposit_screenshot: str | None = Field(default=None, max_length=3_000_000)

    @field_validator("registration_uid")
    @classmethod
    def strip_uid(cls, v: str) -> str:
        return v.strip()

    @field_validator("deposit_screenshot")
    @classmethod
    def check_screenshot(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith("data:image/"):
            raise ValueError("deposit_screenshot must be an image data URL.")
        return v


@app.post("/api/submissions", status_code=201)
def create_submission(
    payload: SubmissionIn,
    background: BackgroundTasks,
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
    screenshot = payload.deposit_screenshot
    if screenshot is None:
        # Resubmission (e.g. after info_required) without a fresh upload
        # keeps the deposit proof already on file.
        previous = (
            db.query(Submission.deposit_screenshot)
            .filter(Submission.user_id == user.id, Submission.deposit_screenshot.isnot(None))
            .order_by(desc(Submission.submitted_at))
            .first()
        )
        screenshot = previous[0] if previous else None
    row = Submission(
        user_id=user.id,
        registration_uid=uid,
        reference=(payload.reference.strip() if payload.reference else None),
        deposit_screenshot=screenshot,
        status="pending_review",
    )
    db.add(row)
    db.flush()
    _audit(db, identity.telegram_id, "submission_created", "submission", row.id, {"status": "pending_review", "has_deposit_screenshot": bool(screenshot)})
    db.commit()
    try:
        db.refresh(row)
    except Exception:
        # The row is already committed; with expire_on_commit=False the
        # in-memory values stay usable, so a failed refresh must not 500.
        logger.exception("Failed to refresh submission after create (id=%s).", row.id)
    # Alert every admin right away (after commit): the submission is stored,
    # so a missed alert only costs a notification, never the submission.
    if settings.admin_telegram_ids:
        background.add_task(
            notify_admins,
            settings.bot_token,
            list(settings.admin_telegram_ids),
            submission_id=row.id,
            registration_uid=row.registration_uid,
            user_telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            submitted_at=row.submitted_at,
            mini_app_url=settings.mini_app_url,
        )
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
    row, error, not_found = change_submission_status(
        db,
        submission_id=submission_id,
        to_status=payload.to_status,
        message=payload.message,
        admin_telegram_id=admin.telegram_id,
        internal_note=payload.internal_note,
    )
    if error is not None:
        raise HTTPException(status_code=404 if not_found else 422, detail=error)
    try:
        db.refresh(row)
    except Exception:
        # The status change is already committed; with
        # expire_on_commit=False the in-memory row stays usable.
        logger.exception("Failed to refresh submission after status change (id=%s).", row.id)
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
