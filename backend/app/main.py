"""VerifyHub API — FastAPI app.

Routes:
  GET  /api/config            public, non-secret config (registration URL, UID rule, project name)
  GET  /api/me                authenticated user + is_admin (derived server-side)
  GET  /api/me/submissions    own submissions only (IDOR-safe: filtered by user_id)
  GET  /api/me/latest         own latest submission, or not_submitted
  GET  /api/prediction        verified users only (server-gated)
  GET  /api/live              public WinGo 1-min ticker (no auth, real data only)
  POST /api/submissions       create, with duplicate + rate-limit protection
  POST /api/me/submissions/{id}/withdraw  owner withdraws their own pending submission
  GET  /api/admin/stats       admin only, derived from real DB rows
  GET  /api/admin/submissions admin only, search / filter / sort
  GET  /api/admin/submissions/{id}  admin only, full UID + history
  POST /api/admin/submissions/{id}/status  admin only, validated transition + audit + notify
  GET  /api/admin/audit       admin only, recent audit events
  POST /telegram/webhook      Telegram bot updates (webhook mode, free tier)

The Telegram bot runs inside this process via webhook — no polling
worker is needed on the free tier. The same handlers as the polling
bot (app.bot) process the updates; see the webhook section
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only
from telegram import Update as TelegramUpdate
from telegram.ext import Application as TelegramApplication

from .auth import get_current_user, get_db_session, get_identity, require_admin
from . import autoverify
from .config import Settings, get_settings
from .db import AuditEvent, ProcessedUpdate, ReviewHistory, Submission, User, get_session_factory, mask_uid
from .bot import close_telegram_client, notify_admins, notify_admins_text, notify_user
from .prediction import TIMER_TYPE_IDS, ProviderError, get_accuracy, get_live, get_prediction, record_signal
from .rate_limit import RateLimitExceeded, limiter
from .security import TelegramIdentity
from .services import ACTIVE_STATUSES, STATUSES, change_submission_status, is_admin, list_db_admins, user_badges, vip_tier

logger = logging.getLogger("verifyhub.api")


# ---------------- Telegram webhook (free tier: bot runs inside the API) ----------------
# The bot is NOT polled here. Telegram delivers updates to
# POST /telegram/webhook, which feeds them to a shared python-telegram-bot
# Application (built with updater=None) using the exact same handlers as
# the polling worker (app.bot). Only ONE transport may be active
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
    from .bot import build_telegram_application

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
    # Daily auto-verification re-check: pending submissions whose deposit
    # was not visible yet (the site publishes a day late) get another
    # chance the next day, without an admin lifting a finger. The loop
    # never raises and never auto-rejects — see app.autoverify. It only
    # starts when credentials are configured; otherwise every
    # submission stays in the admin queue exactly as before.
    auto_verify_task = asyncio.create_task(_auto_verify_loop())
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
    auto_verify_task.cancel()
    try:
        await auto_verify_task
    except asyncio.CancelledError:
        pass
    try:
        await close_telegram_client()
    except Exception:
        logger.exception("Error closing Telegram client.")
    if ptb is not None:
        try:
            await ptb.shutdown()
        except Exception:
            logger.exception("Error shutting down Telegram application.")


async def _auto_verify_loop() -> None:
    """Daily re-check of pending submissions (see app.autoverify).

    Runs once shortly after startup (to catch up after a restart or
    deploy) and then every 24 hours. Users who got auto-verified are
    notified here, after the status change is committed. The loop
    never raises — a failure is logged and the next run tries again.
    """
    settings = get_settings()
    if not settings.autoverify_configured:
        logger.info("Auto-verification is not configured; daily re-check disabled.")
        return
    await asyncio.sleep(60)
    while True:
        try:
            results = await asyncio.to_thread(
                autoverify.check_pending_submissions,
                get_session_factory(settings.database_url),
                settings,
            )
            verified = [r for r in results if r.get("passed")]
            for r in verified:
                tid = r.get("user_telegram_id")
                if tid and settings.bot_token:
                    try:
                        await notify_user(
                            settings.bot_token, tid, "verified",
                            r.get("review_message"), settings.mini_app_url,
                        )
                    except Exception:
                        logger.exception("Auto-verify user notify failed (submission %s).",
                                         r.get("submission_id"))
            if results:
                logger.info("Daily auto-verify run: checked %s, auto-verified %s.",
                            len(results), len(verified))
        except Exception:
            logger.exception("Daily auto-verify run failed; will retry tomorrow.")
        await asyncio.sleep(24 * 3600)


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
    try:
        auto_result = json.loads(s.auto_check_result) if s.auto_check_result else None
    except (ValueError, TypeError):
        auto_result = s.auto_check_result
    d.update(
        {
            "registration_uid": s.registration_uid,
            "deposit_screenshot": s.deposit_screenshot,
            "user_telegram_id": s.user.telegram_id if s.user else None,
            "user_username": s.user.username if s.user else None,
            "user_first_name": s.user.first_name if s.user else None,
            # Automatic verification outcome (app.autoverify): the last
            # check's result dict, or None until the first check runs.
            "auto_check_result": auto_result,
            "auto_checked_at": _iso(s.auto_checked_at),
        }
    )
    return d


async def _process_new_submission(
    *,
    submission_id: int,
    registration_uid: str,
    user_telegram_id: int,
    username: str | None,
    first_name: str | None,
    submitted_at,
    settings: Settings,
) -> None:
    """Post-submit background flow: auto-check first, then notify.

    Runs after the submission is committed, so the 201 response never
    waits on the provider. A clean auto-check verifies the submission
    (via the shared status change, system actor) and the user is told;
    the admins get an information-only alert. Anything else leaves
    the submission pending and the admins get the usual action alert,
    with the auto-check outcome attached so they can see at a glance
    whether the UID was found and what the deposit is so far. Nothing
    here ever raises — a missed notification must not break anything.
    """
    outcome: dict | None = None
    try:
        outcome = await asyncio.to_thread(
            autoverify.run_auto_check_for_submission,
            get_session_factory(settings.database_url),
            submission_id,
            settings,
        )
    except Exception:
        logger.exception("Auto-check background task failed for submission %s.", submission_id)
    note = autoverify.auto_check_summary(outcome)
    admin_ids = _all_admin_ids(settings)
    # Resubmission flag: the admin triaging the alert cannot otherwise
    # tell this user was already decided once without opening the
    # inspector. One line in the alert fixes that.
    prior_note = _prior_submission_note(settings, user_telegram_id, submission_id)
    if outcome and outcome.get("passed"):
        if settings.bot_token:
            try:
                await notify_user(
                    settings.bot_token, user_telegram_id, "verified",
                    outcome.get("review_message"), settings.mini_app_url,
                )
            except Exception:
                logger.exception("Auto-verify user notify failed (submission %s).", submission_id)
        if settings.bot_token and admin_ids:
            try:
                await notify_admins(
                    settings.bot_token,
                    admin_ids,
                    submission_id=submission_id,
                    registration_uid=registration_uid,
                    user_telegram_id=user_telegram_id,
                    username=username,
                    first_name=first_name,
                    submitted_at=submitted_at,
                    mini_app_url=settings.mini_app_url,
                    auto_verified=True,
                    auto_check_note=note,
                    resubmission_note=prior_note,
                )
            except Exception:
                logger.exception("Auto-verify admin notify failed (submission %s).", submission_id)
        return
    if outcome and outcome.get("reason") == "deposit_below_minimum":
        # The UID was found but the deposit is short: tell the user once,
        # so they are not left waiting in silence. Information only —
        # never a verification promise; the daily re-check retries.
        deposit = outcome.get("deposit_total") or 0
        if settings.bot_token:
            try:
                await notify_user(
                    settings.bot_token,
                    user_telegram_id,
                    "pending_review",
                    f"Aapki UID mil gayi hai, deposit abhi ₹{deposit:g} hai. "
                    "₹300 se zyada hone par daily re-check me dobara verify hoga — "
                    "tab tak wait karo, dobara submit karne ki zaroorat nahi.",
                    settings.mini_app_url,
                )
            except Exception:
                logger.exception("Deposit-short user notify failed (submission %s).", submission_id)
    if settings.bot_token and admin_ids:
        try:
            await notify_admins(
                settings.bot_token,
                admin_ids,
                submission_id=submission_id,
                registration_uid=registration_uid,
                user_telegram_id=user_telegram_id,
                username=username,
                first_name=first_name,
                submitted_at=submitted_at,
                mini_app_url=settings.mini_app_url,
                auto_check_note=note,
                resubmission_note=prior_note,
            )
        except Exception:
            logger.exception("Admin notify failed (submission %s).", submission_id)


def _all_admin_ids(settings: Settings) -> list[int]:
    """Env allowlist + bot-added admins.

    The old code alerted only the env allowlist, so a bot-added admin
    could decide in the panel but never heard about new submissions.
    Support tickets already used the combined list; submissions now do
    too. A DB failure falls back to the env list only.
    """
    ids = list(settings.admin_telegram_ids)
    try:
        with get_session_factory(settings.database_url)() as db:
            ids += [r.telegram_id for r in list_db_admins(db)]
    except Exception:
        logger.exception("Could not load bot-added admins; alerting env admins only.")
    return list(dict.fromkeys(ids))


async def _notify_admins_withdrawn(
    *,
    submission_id: int,
    registration_uid: str,
    user_telegram_id: int,
    username: str | None,
    first_name: str | None,
    settings: Settings,
) -> None:
    """Best-effort admin note after a user withdraws a submission. The
    queue simply stops showing the row; this note tells an admin who
    already saw it where it went. Never raises."""
    try:
        who = f"@{username}" if username else (first_name or str(user_telegram_id))
        text = (
            f"Submission #{submission_id} withdraw ho gayi — {who} ne khud "
            f"withdraw ki (UID {registration_uid})."
        )
        await notify_admins_text(settings.bot_token, _all_admin_ids(settings), text)
    except Exception:
        logger.exception("Withdraw admin note failed (submission %s).", submission_id)


def _mark_update_processed(database_url: str, update_id: int) -> bool:
    """Record a webhook update id; True when it was new (process it),
    False when already seen (skip). Prunes rows older than a day. A DB
    failure returns True — fail open, a missed dedup beats a dropped
    real update."""
    try:
        factory = get_session_factory(database_url)
        with factory() as db:
            if db.query(ProcessedUpdate).filter(ProcessedUpdate.update_id == update_id).first() is not None:
                return False
            cutoff = datetime.now(timezone.utc) - timedelta(days=1)
            db.query(ProcessedUpdate).filter(ProcessedUpdate.created_at < cutoff).delete()
            db.add(ProcessedUpdate(update_id=update_id))
            db.commit()
        return True
    except Exception:
        logger.exception("Webhook dedup check failed; processing update anyway.")
        return True


_PRIOR_STATUS_WORDS = {
    "verified": "verify ho gayi thi",
    "rejected": "reject ho gayi thi",
    "info_required": "info mangi gayi thi",
    "under_review": "review me thi",
    "pending_review": "pending thi",
}


def _prior_submission_note(settings: Settings, user_telegram_id: int, submission_id: int) -> str | None:
    """One alert line when this is a resubmission, else None."""
    try:
        with get_session_factory(settings.database_url)() as db:
            user = db.query(User).filter(User.telegram_id == user_telegram_id).first()
            if user is None:
                return None
            prior = (
                db.query(Submission.id, Submission.status)
                .filter(Submission.user_id == user.id, Submission.id != submission_id)
                .order_by(desc(Submission.submitted_at))
                .first()
            )
    except Exception:
        logger.exception("Prior-submission lookup failed (submission %s).", submission_id)
        return None
    if prior is None:
        return None
    prior_id, prior_status = prior
    words = _PRIOR_STATUS_WORDS.get(prior_status, prior_status)
    return f"⚠️ Dobara submission — pehle #{prior_id} {words}."


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
        # The Mini App builds invite links from this (t.me/<bot>?start=ref_<id>).
        # Empty when BOT_USERNAME is not set on the server — the invite card
        # hides itself then instead of building a broken link.
        "bot_username": settings.bot_username,
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
    # Dedup: Telegram retries an update after a 503/slow response, and
    # a retry without tracking re-runs the handler (double support alert,
    # double admin reply). A seen update_id is acknowledged, not
    # re-processed. A DB failure fails open — a missed dedup is better
    # than dropping a real update.
    if update.update_id is not None:
        is_new = await asyncio.to_thread(_mark_update_processed, settings.database_url, update.update_id)
        if not is_new:
            logger.info("Duplicate Telegram update %s ignored.", update.update_id)
            return {"ok": True}
    try:
        await ptb.process_update(update)
    except Exception:
        logger.exception("Failed to process Telegram update.")
        raise HTTPException(status_code=503, detail="Failed to process update; please retry.") from None
    return {"ok": True}


# ---------------- user routes ----------------

@app.get("/api/me")
def me(identity: TelegramIdentity = Depends(get_identity), user: User = Depends(get_current_user), db: Session = Depends(get_db_session), settings: Settings = Depends(get_settings)):
    admin = is_admin(db, identity.telegram_id, settings)
    badges = user_badges(db, identity.telegram_id, is_admin_user=admin)
    submissions = (
        db.query(Submission)
        .options(load_only(Submission.status, Submission.updated_at))
        .filter(Submission.user_id == user.id)
        .order_by(Submission.submitted_at.asc())
        .all()
    )
    verified = any(s.status == "verified" for s in submissions)
    verified_at = next((s.updated_at for s in submissions if s.status == "verified"), None)
    # Honest referral count: referred users who actually got admin-verified.
    # A share click proves nothing — only a verified referral counts.
    referrals_verified = (
        db.query(User.id)
        .join(Submission, Submission.user_id == User.id)
        .filter(User.referred_by_telegram_id == identity.telegram_id, Submission.status == "verified")
        .distinct()
        .count()
    )
    first_seen = user.first_seen_at
    if first_seen is not None and first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=timezone.utc)
    return {
        "telegram_id": identity.telegram_id,
        "username": identity.username,
        "first_name": identity.first_name,
        "is_admin": admin,
        "badges": badges,
        "tier": vip_tier(badges, verified, len(submissions)),
        "verified": verified,
        "verified_at": verified_at.isoformat() if verified_at else None,
        "member_rank": db.query(User).filter(User.id < user.id).count() + 1,
        "total_users": db.query(User).count(),
        "account_age_days": (datetime.now(timezone.utc) - first_seen).days if first_seen else 0,
        "referrals_verified": referrals_verified,
        "referral_link": (
            f"https://t.me/{settings.bot_username}?start=ref_{identity.telegram_id}"
            if settings.bot_username
            else None
        ),
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
    # Run the auto-check, then alert the admins (and the user, if the
    # check verified them) — all after commit, so a missed notification
    # or a provider hiccup only costs a message, never the submission.
    # With auto-verification unconfigured this just sends the admin
    # alert exactly as before.
    background.add_task(
        _process_new_submission,
        submission_id=row.id,
        registration_uid=row.registration_uid,
        user_telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        submitted_at=row.submitted_at,
        settings=settings,
    )
    return _public_submission(row)


@app.post("/api/me/submissions/{submission_id}/withdraw")
def withdraw_submission(
    submission_id: int,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    """The user withdraws their own pending submission (wrong UID typed,
    changed mind). Owner-only, pending_review/under_review only — terminal
    states and other users' rows are refused with 409/403. Withdrawing
    lifts the 409 block, so the user can immediately submit the correct
    UID; the deposit proof carries over to the resubmission exactly like
    after info_required. The daily auto-check only selects
    pending_review rows, so a withdrawn row is never re-checked. Admins
    get a short note (best-effort, after commit)."""
    row = db.query(Submission).filter(Submission.id == submission_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="This submission is not yours.")
    updated, error, _not_found = change_submission_status(
        db,
        submission_id=submission_id,
        to_status="withdrawn",
        message="User ne apni submission withdraw kar li.",
        admin_telegram_id=user.telegram_id,
    )
    if error is not None:
        raise HTTPException(status_code=409, detail=error)
    background.add_task(
        _notify_admins_withdrawn,
        submission_id=submission_id,
        registration_uid=row.registration_uid,
        user_telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        settings=settings,
    )
    return _public_submission(updated)


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


@app.post("/api/admin/autoverify/retry")
async def admin_autoverify_retry(
    admin: TelegramIdentity = Depends(require_admin),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    """Manually re-run the auto-check over pending submissions.

    Same work as the daily loop, but on demand and without the
    20-hour "recently checked" skip, so an admin can force a fresh
    check after a deposit lands. Users who pass are notified here,
    after their status change is committed. Never auto-rejects.
    """
    if not settings.autoverify_configured:
        raise HTTPException(
            status_code=503,
            detail="Auto-verification is not configured. "
            "Set AUTOVERIFY_USERNAME and AUTOVERIFY_PASSWORD on the API service.",
        )
    results = await asyncio.to_thread(
        autoverify.check_pending_submissions,
        get_session_factory(settings.database_url),
        settings,
        max_rows=100,
        min_interval_hours=0,
    )
    verified = [r for r in results if r.get("passed")]
    for r in verified:
        tid = r.get("user_telegram_id")
        if tid and settings.bot_token:
            try:
                await notify_user(
                    settings.bot_token, tid, "verified",
                    r.get("review_message"), settings.mini_app_url,
                )
            except Exception:
                logger.exception("Auto-verify user notify failed (submission %s).",
                                 r.get("submission_id"))
    return {
        "checked": len(results),
        "auto_verified": len(verified),
        "still_pending": len(results) - len(verified),
        "results": [
            {
                "submission_id": r.get("submission_id"),
                "passed": bool(r.get("passed")),
                "reason": r.get("reason"),
                "deposit_total": r.get("deposit_total"),
            }
            for r in results
        ],
    }


@app.get("/api/admin/audit")
def admin_audit(admin: TelegramIdentity = Depends(require_admin), db: Session = Depends(get_db_session), actor: str = Query(default="")):
    """Recent audit events, newest first. ``actor`` filters to one
    Telegram id (per-admin activity). ``meta`` is parsed JSON — the
    broadcast preview text lives there, so the per-admin view can show
    what was actually sent."""
    q = db.query(AuditEvent).order_by(desc(AuditEvent.created_at))
    if actor:
        try:
            actor_id = int(actor)
        except ValueError:
            raise HTTPException(status_code=422, detail="actor must be a Telegram id.") from None
        q = q.filter(AuditEvent.actor_telegram_id == actor_id)
    rows = q.limit(100).all()
    out = []
    for r in rows:
        meta = None
        if r.meta:
            try:
                meta = json.loads(r.meta)
            except Exception:
                meta = r.meta
        out.append(
            {
                "id": r.id,
                "actor_telegram_id": r.actor_telegram_id,
                "action": r.action,
                "record_type": r.record_type,
                "record_id": r.record_id,
                "meta": meta,
                "created_at": _iso(r.created_at),
            }
        )
    return out


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please try again later."})


# Serve the Mini App from the same process in dev (see module docstring).
_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
