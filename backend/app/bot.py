"""VerifyHub Telegram bot — all bot-side code in this one file.

Handlers, user/admin notifications and the Premium custom emoji IDs used
to live in four modules (``bot_handlers``, ``notifications``, ``emoji``
and this entry point); they are merged here so the whole bot is in one
place. Nothing else changed: the API (``app.main``) and the shared
backend (``app.db``, ``app.services``, ``app.config``, ``app.prediction``)
are still imported, not copied.

Shared by polling and webhook modes: the polling worker runs
``python -m app.bot`` (see :func:`main` below) and the webhook endpoint
in the API (``POST /telegram/webhook``) feeds updates to the same
handlers via :func:`build_telegram_application`. Nothing in this module
knows which transport delivered the update.

Handlers are registered with :func:`register_handlers`; use
:func:`build_telegram_application` to build an Application for either
mode (``with_updater=True`` for polling, ``False`` for webhook).
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import html
import io
import logging
import math
import time

import httpx
from sqlalchemy import desc, func
from telegram import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    MessageEntity,
    Update,
    WebAppInfo,
)
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import get_settings
from .db import Admin, SignalRecord, Submission, User, get_session_factory, mask_uid
from .prediction import ProviderError, get_live, get_prediction, record_signal
from .services import (
    ACTIVE_STATUSES,
    STATUSES,
    add_admin,
    change_submission_status,
    is_admin,
    list_db_admins,
    remove_admin,
)

# Telegram Premium custom emoji IDs used by the bot's buttons and messages.
# Resolved from the user's own sets via getStickerSet; the bot owner has
# Telegram Premium, which is what lets the bot use them. Older clients
# ignore a custom emoji icon and show the plain label instead.
EMOJI_CROWN = "6052853159204757225"      # 👑 from emoji_H_M_by_TgEmodziBot
EMOJI_CHART = "5190806721286657692"      # 📊 from FinanceEmoji
EMOJI_DIAMOND = "6053338597883386982"   # 💎 from emoji_H_M_by_TgEmodziBot
EMOJI_THINKING = "6334471179801200139"  # 🧐 from peachgomaemoji
EMOJI_CHECK = "6055128392360010233"      # ✅ from emoji_H_M_by_TgEmodziBot
EMOJI_TARGET = "5310278924616356636"     # 🎯 from FinanceEmoji
EMOJI_CHART_UP = "5197503331215361533"   # 📈 from FinanceEmoji

logger = logging.getLogger("verifyhub.bot")
_notif_logger = logging.getLogger("verifyhub.notifications")
settings = get_settings()

BOT_BOOT_TIME = time.monotonic()
PENDING_PAGE_SIZE = 6
# Same UID rule as the API (settings.uid_pattern, 6–64 chars): the chat
# submit must accept exactly what the Mini App accepts and reject what it
# rejects — one source of truth, so the two can never drift apart.
UID_PATTERN = settings.uid_regex

# Per-user language preference ("hi" = Hinglish default, "en" = English)
_user_lang: dict[int, str] = {}

# One-tap admin review presets for ultra-fast moderation
REJECT_PRESETS: dict[str, tuple[str, str]] = {
    "uid_not_found": (
        "❌ UID Not Found",
        "The UID you submitted was not found under our official registration link. Please check your UID and submit again.",
    ),
    "wrong_link": (
        "❌ Not Under Official Link",
        "Your account was not registered using the official registration link. Please register a new account via the official link and submit that UID.",
    ),
    "duplicate": (
        "❌ Duplicate Submission",
        "This UID has already been submitted or linked to another account.",
    ),
}

INFO_PRESETS: dict[str, tuple[str, str]] = {
    "deposit": (
        "ℹ️ First Deposit Needed",
        "Please complete your first deposit on the official platform so we can verify your active UID.",
    ),
    "recheck_uid": (
        "ℹ️ Recheck Numeric UID",
        "Please copy your exact numeric UID from your account profile page and submit it again.",
    ),
    "activity": (
        "ℹ️ Account Activity Required",
        "Your UID is registered but shows no recent activity yet. Please complete basic account activation and update your submission.",
    ),
}

# --- Notifications: send AFTER the DB transaction commits. Failures here
# never roll back a status change — they are logged and the admin response
# still succeeds. No tokens are logged.
STATUS_COPY = {
    "pending_review": "Your submission is now pending review.",
    "under_review": "Your submission is now under review by an administrator.",
    "verified": "Good news — your submission has been marked as verified by an administrator.",
    "rejected": "Your submission was not approved. Please check the review message in the panel.",
    "info_required": "An administrator needs additional information. Please check the review message in the panel.",
}

STATUS_LABELS = {
    "not_submitted": "Not submitted",
    "pending_review": "Pending review",
    "under_review": "Under review",
    "verified": "Verified (administrator-reviewed)",
    "rejected": "Rejected",
    "info_required": "Additional information required",
}

STATUS_ICONS = {
    "not_submitted": "⚪",
    "pending_review": "⏳",
    "under_review": "🔎",
    "verified": "✅",
    "rejected": "❌",
    "info_required": "ℹ️",
}


@contextmanager
def _db_session():
    """Yield a SQLAlchemy session and guarantee cleanup on exit."""
    db = get_session_factory(settings.database_url)()
    try:
        yield db
    finally:
        db.close()


def _get_lang(user_id: int) -> str:
    return _user_lang.get(user_id, "hi")


def _toggle_lang(user_id: int) -> str:
    curr = _user_lang.get(user_id, "hi")
    nxt = "en" if curr == "hi" else "hi"
    if len(_user_lang) > 20_000:
        _user_lang.clear()
    _user_lang[user_id] = nxt
    return nxt


def _format_uptime() -> str:
    elapsed = int(max(0.0, time.monotonic() - BOT_BOOT_TIME))
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


def _sparkline(values: list[int]) -> str:
    """Render a compact Unicode bar chart for 7-day trends."""
    if not values or max(values) == 0:
        return "▁" * max(1, len(values))
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values)
    return "".join(blocks[min(7, int((v / peak) * 7))] for v in values)


async def _post_telegram_json(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    """POST to the Bot API with automatic 429 retry_after handling."""
    for attempt in range(2):
        resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return True
        if resp.status_code == 429 and attempt == 0:
            try:
                retry_after = float(resp.json().get("parameters", {}).get("retry_after", 1))
            except Exception:
                retry_after = 1.0
            await asyncio.sleep(min(retry_after + 0.5, 5.0))
            continue
        _notif_logger.warning("Telegram notify failed: status=%s", resp.status_code)
        return False
    return False


async def notify_user(
    bot_token: str,
    telegram_id: int,
    status: str,
    message: str | None,
    mini_app_url: str,
) -> bool:
    if not bot_token:
        return False
    icon = STATUS_ICONS.get(status, "🔔")
    headline = STATUS_COPY.get(status, f"Your submission status changed to {status}.")
    text = f"{icon} <b>{html.escape(headline)}</b>"
    if message:
        # The payload uses parse_mode=HTML, so escape the admin's message:
        # a raw "<" or "&" would make Telegram reject the whole notification.
        text += f"\n\n<b>Review message:</b>\n<i>{html.escape(message)}</i>"
    text += (
        "\n\nVerification does not guarantee financial recovery. "
        "Open the verification panel to see the full status."
    )
    buttons = []
    if mini_app_url:
        buttons.append([{"text": "Open Verification Panel", "web_app": {"url": mini_app_url}}])
        if status == "verified":
            sep = "&" if "?" in mini_app_url else "?"
            predict_url = f"{mini_app_url}{sep}startapp=prediction"
            buttons.append([{"text": "🎯 Open Predictions", "web_app": {"url": predict_url}}])
    buttons.append([
        {"text": "📊 Check My Status", "callback_data": "__status__"},
        {"text": "🎯 Live Signal", "callback_data": "__signal__"},
    ])
    if status in ("rejected", "info_required"):
        buttons.append([
            {"text": "🚀 Resubmit UID in Chat", "callback_data": "__submit_uid__"},
            {"text": "💬 Contact Admin", "callback_data": "__support__"},
        ])
    payload: dict = {
        "chat_id": telegram_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            return await _post_telegram_json(
                client,
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                payload,
            )
    except Exception:
        _notif_logger.exception("Telegram notify error (telegram_id redacted from payload)")
        return False


async def notify_admins(
    bot_token: str,
    admin_ids: list[int],
    *,
    submission_id: int,
    registration_uid: str,
    user_telegram_id: int,
    username: str | None,
    first_name: str | None,
    submitted_at,
    mini_app_url: str | None,
) -> int:
    """Alert every admin the moment a user submits a UID, with one-tap actions.

    Returns how many admins were reached. Failures are logged, never raised:
    the submission is already stored; a missed alert must not break it.
    The buttons carry callback_data the bot's admin handler understands
    (``admin:approve:<id>:n`` etc.), so the admin decides without opening
    the Mini App.
    """
    if not bot_token or not admin_ids:
        return 0
    who = f"@{username}" if username else (first_name or str(user_telegram_id))
    when = submitted_at.strftime("%Y-%m-%d %H:%M UTC") if submitted_at else "—"
    text = (
        f"🆕 New submission #{submission_id}\n\n"
        f"UID: {registration_uid}\n"
        f"User: {who} ({user_telegram_id})\n"
        f"Sent: {when}\n\n"
        "Tap to decide, pick a preset reason, or inspect full user history."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"admin:approve:{submission_id}:n"},
                {"text": "❌ Reject", "callback_data": f"admin:reject:{submission_id}:n"},
                {"text": "ℹ️ Need info", "callback_data": f"admin:info:{submission_id}:n"},
            ],
            [
                {"text": "🔎 Under Review", "callback_data": f"admin:review:{submission_id}:n"},
                {"text": "⚡ Presets / Note", "callback_data": f"admin:view:{submission_id}"},
                {"text": "↩️ Message User", "callback_data": f"admin:reply:{user_telegram_id}"},
            ],
        ]
    }
    if mini_app_url:
        keyboard["inline_keyboard"].append(
            [{"text": "Open Verification Panel", "web_app": {"url": mini_app_url}}]
        )
    sent = 0
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            for admin_id in dict.fromkeys(admin_ids):
                payload: dict = {"chat_id": admin_id, "text": text, "reply_markup": keyboard}
                try:
                    if await _post_telegram_json(client, url, payload):
                        sent += 1
                except Exception:
                    _notif_logger.exception("Telegram admin notify error (admin id redacted)")
    except Exception:
        _notif_logger.exception("Telegram admin notify client error")
    return sent


# Per-user last-action timestamps for the rate limit (see _too_fast).
_last_action: dict[int, float] = {}
# Monotonic timestamp of the last admin error alert (see _on_error).
_last_error_alert: float = 0.0


def _too_fast(user_id: int, window: float = 0.7) -> bool:
    """True when this user acted within the last `window` seconds.

    A tiny abuse/double-tap guard for /start and button taps: normal use
    is slower than 0.7s, so nothing legitimate is ever blocked. The table is
    capped so a very large user base cannot grow it without bound.
    """
    now = time.monotonic()
    if now - _last_action.get(user_id, 0.0) < window:
        return True
    if len(_last_action) > 10_000:
        _last_action.clear()
    _last_action[user_id] = now
    return False


async def _edit_or_reply(query, text: str, entities=None, reply_markup=None) -> None:
    """Edit the tapped message in place; fall back to a new message when
    the edit is impossible (message too old, or already showing this text).

    Keeps inline menus from spamming a new message on every tap.
    """
    try:
        await query.edit_message_text(text, entities=entities, reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.debug("Could not edit message, replying instead: %s", e)
        if query.message:
            await query.message.reply_text(text, entities=entities, reply_markup=reply_markup)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler: log the failure, alert an admin (throttled).

    PTB calls this for any unhandled exception in a handler. The alert is
    throttled to one per 10 minutes so a bad deploy cannot spam the admin.
    """
    logger.exception("Bot handler failed.", exc_info=context.error)
    global _last_error_alert
    now = time.monotonic()
    if now - _last_error_alert < 600:
        return
    _last_error_alert = now
    try:
        for admin_id in settings.admin_telegram_ids:
            await context.bot.send_message(
                admin_id,
                f"⚠️ VerifyHub bot error: {type(context.error).__name__}: {context.error}",
            )
    except Exception:
        logger.debug("Could not alert admin about the bot error.", exc_info=True)


def _render(segments):
    """Build (text, entities) from (text, kind) segments.

    kind is None (plain), "bold", "italic", "code", or ("custom", id).
    A "custom" segment's text is the Unicode placeholder the premium emoji
    replaces. Offsets/lengths are UTF-16 code units, as the Bot API requires
    (astral-plane emoji count as 2). Messages built this way are sent with
    ``entities=`` and no parse_mode, so their text is literal — no HTML
    escaping is ever needed.
    """
    text = ""
    entities = []
    for part, kind in segments:
        if not part:
            continue
        offset = len(text.encode("utf-16-le")) // 2
        length = len(part.encode("utf-16-le")) // 2
        text += part
        if kind == "bold":
            entities.append(MessageEntity(MessageEntity.BOLD, offset, length))
        elif kind == "italic":
            entities.append(MessageEntity(MessageEntity.ITALIC, offset, length))
        elif kind == "code":
            entities.append(MessageEntity(MessageEntity.CODE, offset, length))
        elif isinstance(kind, tuple) and kind[0] == "custom":
            entities.append(
                MessageEntity(MessageEntity.CUSTOM_EMOJI, offset, length, custom_emoji_id=kind[1])
            )
    return text, entities


def _welcome_text(
    first_name: str | None = None,
    verified: bool = False,
    lang: str = "hi",
):
    if lang == "en":
        greeting = f"Welcome, {first_name}! " if first_name else ""
        segments = [
            ("🔥 ", None),
            (f"{greeting}VerifyHub — One UID, Instant Clarity.", "bold"),
            ("\n\n", None),
            ("✅", ("custom", EMOJI_CHECK)),
            (" Submit your UID via the Panel or directly in chat.", None),
            ("\n", None),
            ("🎯", ("custom", EMOJI_TARGET)),
            (" Unlock live WinGo 30s & 1m signals + streak analytics once verified.", None),
            ("\n", None),
            ("📈", ("custom", EMOJI_CHART_UP)),
            (" Track real-time win rates, VIP tier badges & instant status alerts.", None),
        ]
        if verified:
            segments += [
                ("\n\n", None),
                ("💎", ("custom", EMOJI_DIAMOND)),
                (" Your account is Verified — Signals & Predictions are unlocked!", "bold"),
            ]
        else:
            segments.append(("\n\nTap a button below to submit your UID or check your status.", None))
        return _render(segments)

    greeting = f"Namaste {first_name}! " if first_name else ""
    segments = [
        ("🔥 ", None),
        (f"{greeting}VerifyHub — ek baar, ek faisla.", "bold"),
        ("\n\n", None),
        ("✅", ("custom", EMOJI_CHECK)),
        (" Apna UID Panel se ya seedha chat mein bhejo — admin turant check karega.", None),
        ("\n", None),
        ("🎯", ("custom", EMOJI_TARGET)),
        (" Verify hote hi live WinGo 30s & 1m signals + streak analytics khul jayenge.", None),
        ("\n", None),
        ("📈", ("custom", EMOJI_CHART_UP)),
        (" Real-time win rate, VIP tier badges aur instant status alerts.", None),
    ]
    if verified:
        segments += [
            ("\n\n", None),
            ("💎", ("custom", EMOJI_DIAMOND)),
            (" Aapka account Verified hai — Predictions aur Live Signal ready hain!", "bold"),
        ]
    else:
        segments.append(("\n\nNeeche wala button dabao aur verification panel ya quick submit kholo.", None))
    return _render(segments)


def _help_text(admin: bool = False, lang: str = "hi"):
    segments = [
        ("👑", ("custom", EMOJI_CROWN)),
        (" ", None),
        ("VerifyHub — Complete Guide" if lang == "en" else "VerifyHub — Kaise Kaam Karta Hai", "bold"),
        ("\n\n", None),
        ("1.", "bold"),
        (" Register on the official site and copy your numeric UID.", None),
        ("\n", None),
        ("2.", "bold"),
        (" Tap Open Verification Panel (or 🚀 Quick Submit UID right in chat).", None),
        ("\n", None),
        ("3.", "bold"),
        (" An admin reviews it — track all 3 verification windows live.", None),
        ("\n\n", None),
        ("Verified", "bold"),
        (" = admin-approved. No guarantee of financial recovery, and not provider-confirmed.", None),
        ("\nVerified users unlock the Prediction Mini App & Live Signal Hub (WinGo 30s + 1m with streak tape & confidence meter).", None),
        ("\n\n🔒 Security: We NEVER ask for passwords, OTPs, or recovery phrases.", None),
        ("\n\n", None),
        ("User Features", "bold"),
        ("\n• Open Verification Panel — full Mini App experience", None),
        ("\n• 🚀 Quick Submit UID — send your UID directly in Telegram chat", None),
        ("\n• 📊 Check My Status — 3-step progress bar, submission history & admin notes", None),
        ("\n• 🎯 Signal Hub — switch between 1m & 30s WinGo signals, streaks & last 10 outcomes", None),
        ("\n• 👤 My Profile — VIP tier, join rank, earned badges & direct admin support", None),
    ]
    if admin:
        segments += [
            ("\n\n", None),
            ("🛡 Admin Super-Tools", "bold"),
            ("\n• Admin Control Center — intake metrics, approval rate, 7-day trend & funnel", None),
            ("\n• Paginated Pending Queue — 1-tap Approve/Reject/Info, Preset reasons, Custom notes & Bulk Approve", None),
            ("\n• Search & Inspect — lookup any UID, Telegram ID, or #SubmissionID + direct user messaging", None),
            ("\n• Deep Analytics & Health — 7-day sparklines, turnaround time & DB ping", None),
            ("\n• Dual CSV Exports — download Submissions CSV or Users CSV in 1 tap", None),
            ("\n• Pro Broadcast Studio — target All, Verified, Pending, or Rejected users with auto-pin option", None),
        ]
    return _render(segments)


def _is_admin(telegram_id: int) -> bool:
    """Server-side admin check: env allowlist OR the bot-added admins table."""
    if settings.is_admin(telegram_id):
        return True
    with _db_session() as db:
        return is_admin(db, telegram_id, settings)


def _all_admin_ids() -> list[int]:
    """Combined list of config admin IDs and DB admin IDs."""
    ids = list(settings.admin_telegram_ids)
    with _db_session() as db:
        ids += [r.telegram_id for r in list_db_admins(db)]
    return list(dict.fromkeys(ids))


def _is_user_verified(telegram_id: int) -> bool:
    """True when the user has at least one verified submission."""
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return False
        return (
            db.query(Submission)
            .filter(Submission.user_id == user.id, Submission.status == "verified")
            .first()
            is not None
        )


def _badges_for(telegram_id: int) -> list[str]:
    """Badges earned from real data only — nothing invented or bought.

    👑 Admin — platform administrator.
    ✅ Verified — submission approved by an admin.
    ⚡ First Try — verified on the very first submission.
    ⭐ Early Bird — among the first 10 users ever.
    🏆 OG Member — among the first 50 users ever.
    📅 Veteran — account older than 30 days.
    🔁 Comeback — submitted more than once.
    """
    badges: list[str] = []
    if _is_admin(telegram_id):
        badges.append("👑 Admin")
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return badges
        submissions = (
            db.query(Submission)
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


def _vip_tier(badges: list[str], verified: bool, total_submissions: int) -> str:
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


def _predict_url() -> str | None:
    """Mini App URL that opens straight on the Prediction tab."""
    if not settings.mini_app_url:
        return None
    sep = "&" if "?" in settings.mini_app_url else "?"
    return settings.mini_app_url + sep + "startapp=prediction"


def _keyboard(admin: bool = False, user_id: int | None = None) -> InlineKeyboardMarkup:
    """Rich main menu with Mini App, Quick In-Chat Submit, Status, Signal Hub, Profile, Stats & Language."""
    lang = _get_lang(user_id) if user_id else "hi"
    rows = []
    if settings.mini_app_url:
        rows.append([
            InlineKeyboardButton(
                "Open Verification Panel",
                web_app=WebAppInfo(url=settings.mini_app_url),
                style="primary",
                icon_custom_emoji_id=EMOJI_CROWN,
            )
        ])
    elif settings.registration_url:
        rows.append([
            InlineKeyboardButton(
                "🌐 Official Registration Site",
                url=settings.registration_url,
            )
        ])

    middle = [
        InlineKeyboardButton(
            "Check My Status",
            callback_data="__status__",
            style="success",
            icon_custom_emoji_id=EMOJI_CHART,
        )
    ]
    predict_url = _predict_url()
    if predict_url:
        middle.append(
            InlineKeyboardButton(
                "Open Predictions",
                web_app=WebAppInfo(url=predict_url),
                icon_custom_emoji_id=EMOJI_DIAMOND,
            )
        )
    else:
        middle.append(
            InlineKeyboardButton(
                "🚀 Quick Submit UID",
                callback_data="__submit_uid__",
                icon_custom_emoji_id=EMOJI_CHECK,
            )
        )
    rows.append(middle)

    rows.append([
        InlineKeyboardButton(
            "🎯 Signal Hub",
            callback_data="__signal__:1m",
            icon_custom_emoji_id=EMOJI_TARGET,
        ),
        InlineKeyboardButton(
            "🚀 Submit UID",
            callback_data="__submit_uid__",
            icon_custom_emoji_id=EMOJI_CHECK,
        ),
    ])

    lang_label = "🇬🇧 EN" if lang == "hi" else "🇮🇳 HI"
    rows.append([
        InlineKeyboardButton(
            "👤 Profile",
            callback_data="__profile__",
            icon_custom_emoji_id=EMOJI_CROWN,
        ),
        InlineKeyboardButton(
            "📈 Stats",
            callback_data="__stats__",
            icon_custom_emoji_id=EMOJI_CHART_UP,
        ),
        InlineKeyboardButton(
            "Help",
            callback_data="__help__",
            icon_custom_emoji_id=EMOJI_THINKING,
        ),
        InlineKeyboardButton(
            lang_label,
            callback_data="__lang__",
        ),
    ])
    if admin:
        rows.append([
            InlineKeyboardButton(
                "🛡 Admin Control Center",
                callback_data="__admin__",
                icon_custom_emoji_id=EMOJI_CROWN,
            )
        ])
    return InlineKeyboardMarkup(rows)


def _broadcast_targets(mode: str | bool) -> list[int]:
    """Telegram ids of the broadcast audience.

    Supports legacy bool (`True` = verified, `False` = all) as well as
    `"all"`, `"verified"`, `"pending"` (unverified), and `"rejected"` (rejected/info_required).
    """
    if isinstance(mode, bool):
        mode = "verified" if mode else "all"
    with _db_session() as db:
        if mode == "verified":
            rows = (
                db.query(User.telegram_id)
                .join(Submission, Submission.user_id == User.id)
                .filter(Submission.status == "verified")
                .distinct()
                .all()
            )
            return [r[0] for r in rows]
        if mode == "pending":
            verified_subq = (
                db.query(Submission.user_id)
                .filter(Submission.status == "verified")
                .distinct()
            )
            rows = (
                db.query(User.telegram_id)
                .filter(~User.id.in_(verified_subq))
                .all()
            )
            return [r[0] for r in rows]
        if mode == "rejected":
            rows = (
                db.query(User.telegram_id)
                .join(Submission, Submission.user_id == User.id)
                .filter(Submission.status.in_(("rejected", "info_required")))
                .distinct()
                .all()
            )
            return [r[0] for r in rows]
        rows = db.query(User.telegram_id).all()
        return [r[0] for r in rows]


def _broadcast_counts() -> tuple[int, int]:
    """(all users, verified users) for backward-compatible broadcast preview."""
    all_ids = _broadcast_targets("all")
    verified_ids = _broadcast_targets("verified")
    return len(all_ids), len(verified_ids)


async def _send_broadcast(
    bot,
    text: str,
    target_ids: list[int],
    entities=None,
    pin_message: bool = False,
    progress_cb=None,
) -> tuple[int, int, int]:
    """Send one message to every id; returns (delivered, blocked, failed).

    Supports entity preservation and optional auto-pinning in user chats.
    """
    delivered = blocked = failed = 0
    total = len(target_ids)
    for idx, chat_id in enumerate(target_ids, 1):
        try:
            sent_msg = await bot.send_message(chat_id, text, entities=entities)
            delivered += 1
            if pin_message and sent_msg:
                try:
                    await bot.pin_chat_message(chat_id, sent_msg.message_id, disable_notification=True)
                except Exception:
                    pass
        except Forbidden:
            blocked += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                sent_msg = await bot.send_message(chat_id, text, entities=entities)
                delivered += 1
                if pin_message and sent_msg:
                    try:
                        await bot.pin_chat_message(chat_id, sent_msg.message_id, disable_notification=True)
                    except Exception:
                        pass
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        if progress_cb and idx % 20 == 0 and idx < total:
            try:
                await progress_cb(idx, total, delivered, blocked, failed)
            except Exception:
                pass
        await asyncio.sleep(0.05)
    return delivered, blocked, failed


def _window_lines(status: str) -> list[str]:
    """The three verification windows as text lines (✓ done, … in progress, ○ waiting)."""
    w2 = "…" if status in ("pending_review", "under_review") else "✓"
    if status in ("verified", "rejected", "info_required"):
        w3 = "✓"
    elif status == "under_review":
        w3 = "…"
    else:
        w3 = "○"
    return [f"01 Received ✓", f"02 Under review {w2}", f"03 Decision {w3}"]


def _progress_bar(status: str) -> str:
    """Visual progress bar for the verification pipeline."""
    mapping = {
        "not_submitted": "[░░░░░░] 0/3",
        "pending_review": "[██░░░░] 1/3",
        "under_review": "[████░░] 2/3",
        "info_required": "[████░░] 2/3 (Action Needed)",
        "verified": "[██████] 3/3 ✅",
        "rejected": "[██████] 3/3 ❌",
    }
    return mapping.get(status, "[██░░░░] 1/3")


def _status_card(
    status: str,
    row: Submission | None,
    badges: list[str] | None = None,
    history: list[Submission] | None = None,
) -> tuple[str, list]:
    """Compact status card: badge word, masked UID, sent time, three windows & history."""
    header = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("Your verification status", "bold"),
    ]
    if row is None:
        segments = header + [
            ("\n\nStatus: ", None),
            ("Not submitted", "bold"),
            (f"\nProgress: {_progress_bar('not_submitted')}", None),
            ("\n\nYou have not submitted a UID yet.\nTap 🚀 Quick Submit UID or Open Verification Panel below to send your UID.", None),
        ]
        if badges:
            segments += [("\n\n", None), ("Badges: ", "bold"), (" · ".join(badges), None)]
        return _render(segments)

    label = STATUS_LABELS.get(status, status)
    icon = STATUS_ICONS.get(status, "🔔")
    segments = header + [
        (f"\n\nStatus: {icon} ", None),
        (label, "bold"),
        (f"\nProgress: {_progress_bar(status)}", None),
        ("\nSubmission: ", None),
        (f"#{row.id}", "code"),
        ("  ·  UID: ", None),
        (mask_uid(row.registration_uid), "code"),
        ("\nSent: ", None),
        (row.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if row.submitted_at else "—", None),
    ]
    if getattr(row, "updated_at", None) and row.updated_at != row.submitted_at:
        segments += [
            ("\nUpdated: ", None),
            (row.updated_at.strftime("%Y-%m-%d %H:%M UTC"), None),
        ]
    segments.append(("\n\n", None))
    for i, line in enumerate(_window_lines(status)):
        if i:
            segments.append(("\n", None))
        segments.append((line, None))
    if row.review_message:
        segments.append(("\n\n", None))
        segments.append(("Admin note:", "bold"))
        segments.append((f"\n{row.review_message}", "italic"))
    if history and len(history) > 1:
        segments.append(("\n\n", None))
        segments.append((f"Recent submissions ({len(history)}):", "bold"))
        for h in history[:4]:
            h_when = h.submitted_at.strftime("%m-%d %H:%M") if h.submitted_at else "—"
            h_icon = STATUS_ICONS.get(h.status, "•")
            segments.append(
                (f"\n{h_icon} #{h.id} · {mask_uid(h.registration_uid)} · {h_when}", None)
            )
    if badges:
        segments += [("\n\n", None), ("Badges: ", "bold"), (" · ".join(badges), None)]
    return _render(segments)


def _profile_card(telegram_id: int, first_name: str | None = None, admin: bool = False) -> tuple[str, list, InlineKeyboardMarkup]:
    """Rich user profile card with VIP Tier, Join Rank, Account Age, Submission Summary & Support."""
    badges = _badges_for(telegram_id)
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        total_users = db.query(User).count()
        if user is not None:
            rank = db.query(User).filter(User.id <= user.id).count()
            subs = (
                db.query(Submission)
                .filter(Submission.user_id == user.id)
                .order_by(desc(Submission.submitted_at))
                .all()
            )
            first_seen = user.first_seen_at
        else:
            rank = None
            subs = []
            first_seen = None

    verified = any(s.status == "verified" for s in subs)
    tier = _vip_tier(badges, verified, len(subs))
    latest = subs[0] if subs else None
    age_days = 0
    if first_seen is not None:
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - first_seen).days)

    segments = [
        ("👑", ("custom", EMOJI_CROWN)),
        (" ", None),
        (f"Member Profile — {first_name or telegram_id}", "bold"),
        ("\n\nTier: ", None),
        (tier, "bold"),
        ("\nTelegram ID: ", None),
        (str(telegram_id), "code"),
    ]
    if rank is not None:
        segments.append((f"\nMember Rank: #{rank} of {total_users}  ·  Account Age: {age_days}d", None))
    segments.append((f"\nTotal Submissions: {len(subs)}", None))
    if latest:
        segments += [
            ("\nActive UID: ", None),
            (mask_uid(latest.registration_uid), "code"),
            (f" ({STATUS_LABELS.get(latest.status, latest.status)})", None),
        ]
    if badges:
        segments += [("\n\nEarned Badges:\n", "bold"), (" · ".join(badges), None)]
    else:
        segments.append(("\n\nEarned Badges: None yet — submit & verify your UID to unlock!", None))

    text, entities = _render(segments)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Submit / Update UID", callback_data="__submit_uid__", icon_custom_emoji_id=EMOJI_CHECK),
            InlineKeyboardButton("📊 Status Card", callback_data="__status__", icon_custom_emoji_id=EMOJI_CHART),
        ],
        [
            InlineKeyboardButton("💬 Contact Admin / Appeal", callback_data="__support__"),
            InlineKeyboardButton("⬅️ Main Menu", callback_data="__home__"),
        ],
    ])
    return text, entities, kb


def _latest_status(telegram_id: int) -> tuple[str, Submission | None, User | None]:
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return "not_submitted", None, None
        row = (
            db.query(Submission)
            .filter(Submission.user_id == user.id)
            .order_by(desc(Submission.submitted_at))
            .first()
        )
        return (row.status if row else "not_submitted"), row, user


def _user_submissions(telegram_id: int, limit: int = 5) -> list[Submission]:
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return []
        return (
            db.query(Submission)
            .filter(Submission.user_id == user.id)
            .order_by(desc(Submission.submitted_at))
            .limit(limit)
            .all()
        )


async def _submit_uid_in_chat(
    user_tg,
    raw_uid: str,
) -> tuple[str, list, InlineKeyboardMarkup, Submission | None]:
    """Allow a user to submit a UID directly in Telegram chat and notify all admins."""
    uid = raw_uid.strip()
    admin = _is_admin(user_tg.id)
    if not UID_PATTERN.match(uid):
        segments = [
            ("❌ ", None),
            ("Invalid UID format.", "bold"),
            ("\n\nPlease send only your Registration UID (6–64 characters, letters, numbers, dash or underscore — no spaces or links).", None),
        ]
        text, entities = _render(segments)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="__home__")]])
        return text, entities, kb, None

    now = datetime.now(timezone.utc)
    with _db_session() as db:
        user = db.query(User).filter(User.telegram_id == user_tg.id).first()
        if user is None:
            user = User(telegram_id=user_tg.id)
            if hasattr(User, "username"):
                setattr(user, "username", user_tg.username)
            if hasattr(User, "first_name"):
                setattr(user, "first_name", user_tg.first_name)
            if hasattr(User, "first_seen_at"):
                setattr(user, "first_seen_at", now)
            db.add(user)
            db.flush()
        else:
            if hasattr(user, "username") and user_tg.username:
                setattr(user, "username", user_tg.username)
            if hasattr(user, "first_name") and user_tg.first_name:
                setattr(user, "first_name", user_tg.first_name)

        # Check if user already has an active pending/under_review submission
        active_sub = (
            db.query(Submission)
            .filter(Submission.user_id == user.id, Submission.status.in_(ACTIVE_STATUSES))
            .order_by(desc(Submission.submitted_at))
            .first()
        )
        if active_sub is not None:
            segments = [
                ("⏳ ", None),
                (f"Submission #{active_sub.id} is already in the review queue!", "bold"),
                (f"\n\nActive UID: {mask_uid(active_sub.registration_uid)}\nStatus: {STATUS_LABELS.get(active_sub.status, active_sub.status)}\n\nPlease wait for an administrator to review your active submission.", None),
            ]
            text, entities = _render(segments)
            return text, entities, _keyboard(admin=admin, user_id=user_tg.id), None

        sub = Submission(
            user_id=user.id,
            registration_uid=uid,
            status="pending_review",
        )
        if hasattr(Submission, "submitted_at"):
            setattr(sub, "submitted_at", now)
        if hasattr(Submission, "updated_at"):
            setattr(sub, "updated_at", now)
        db.add(sub)
        db.commit()
        db.refresh(sub)
        sub_id = sub.id
        sub_at = getattr(sub, "submitted_at", now)

    # Notify all admins (config + DB admins)
    await notify_admins(
        settings.bot_token,
        _all_admin_ids(),
        submission_id=sub_id,
        registration_uid=uid,
        user_telegram_id=user_tg.id,
        username=user_tg.username,
        first_name=user_tg.first_name,
        submitted_at=sub_at,
        mini_app_url=settings.mini_app_url,
    )

    segments = [
        ("✅", ("custom", EMOJI_CHECK)),
        (" ", None),
        (f"Submission #{sub_id} received!", "bold"),
        ("\n\nUID: ", None),
        (mask_uid(uid), "code"),
        ("\nStatus: ", None),
        ("Pending review", "bold"),
        (f"\nProgress: {_progress_bar('pending_review')}", None),
        ("\n\nOur administrators have been alerted and will review your UID shortly. You will receive an instant notification here as soon as a decision is made.", None),
    ]
    text, entities = _render(segments)
    return text, entities, _keyboard(admin=admin, user_id=user_tg.id), sub


def _signal_stats_summary(rows: list[SignalRecord]) -> dict:
    """Compute settled count, win rate, last-20 rolling rate, current streak, and last-10 tape."""
    settled = [r for r in rows if r.correct is not None]
    # Ensure chronological order (oldest -> newest) for streak & tape
    settled.sort(key=lambda r: getattr(r, "id", 0))
    total = len(settled)
    wins = sum(1 for r in settled if r.correct)
    acc = round(wins / total * 100, 1) if total else 0.0

    recent_20 = settled[-20:]
    acc_20 = round(sum(1 for r in recent_20 if r.correct) / len(recent_20) * 100, 1) if recent_20 else 0.0

    # Current streak from newest backwards
    streak_count = 0
    streak_type: bool | None = None
    for r in reversed(settled):
        if streak_type is None:
            streak_type = bool(r.correct)
            streak_count = 1
        elif bool(r.correct) == streak_type:
            streak_count += 1
        else:
            break

    best_win_streak = 0
    cur_w = 0
    for r in settled:
        if r.correct:
            cur_w += 1
            if cur_w > best_win_streak:
                best_win_streak = cur_w
        else:
            cur_w = 0

    tape = "".join("✅" if r.correct else "❌" for r in settled[-10:])
    return {
        "total": total,
        "wins": wins,
        "acc": acc,
        "acc_20": acc_20,
        "streak_count": streak_count,
        "streak_win": bool(streak_type),
        "best_win_streak": best_win_streak,
        "tape": tape or "—",
    }


def _confidence_meter(pct: float) -> str:
    filled = max(0, min(10, round(pct / 10)))
    return "█" * filled + "░" * (10 - filled)


def _signal_preview_card(
    telegram_id: int,
    admin: bool = False,
    timer: str = "1m",
) -> tuple[str, list, InlineKeyboardMarkup]:
    """In-bot live WinGo signal & streak analytics hub with 1m / 30s switcher."""
    if timer not in ("1m", "30s"):
        timer = "1m"
    verified = admin or _is_user_verified(telegram_id)
    if not verified:
        status, _, _ = _latest_status(telegram_id)
        segments = [
            ("🎯", ("custom", EMOJI_TARGET)),
            (" ", None),
            ("WinGo Signal Hub — Verified Members Only", "bold"),
            ("\n\nYour current status: ", None),
            (STATUS_LABELS.get(status, status), "bold"),
            ("\n\nSubmit your UID via 🚀 Quick Submit UID or the Verification Panel. As soon as an admin verifies your UID, both the in-chat Signal Hub (1m & 30s) and the Predictions Mini App unlock automatically.", None),
        ]
        text, entities = _render(segments)
        return text, entities, _keyboard(admin=admin, user_id=telegram_id)

    with _db_session() as db:
        rows_1m = db.query(SignalRecord).filter(SignalRecord.timer == "1m").all()
        rows_30s = db.query(SignalRecord).filter(SignalRecord.timer == "30s").all()

    s1 = _signal_stats_summary(rows_1m)
    s30 = _signal_stats_summary(rows_30s)
    active_stats = s1 if timer == "1m" else s30

    segments = [
        ("🎯", ("custom", EMOJI_TARGET)),
        (" ", None),
        (f"VerifyHub Signal Hub — WinGo {timer.upper()}", "bold"),
        ("\n\n", None),
    ]
    try:
        # Live signal for the NEXT period, from the same deterministic
        # engine the Mini App uses. get_live() cannot serve this: it only
        # returns the 1m ticker (period + countdown), never a pick — and
        # it takes no timer argument, so get_live(timer) raised on every
        # tap and the hub always showed "syncing". get_prediction works
        # for both timers and is the pick's real source.
        pred = get_prediction(timer)
    except ProviderError as e:
        segments.append((f"Live feed notice: {e}\n", "italic"))
        pred = None
    except Exception:
        segments.append(("Live feed: syncing next period…\n", "italic"))
        pred = None
    if pred is not None:
        sig = pred.get("signal")
        current_period = str(pred.get("current_period", "—"))
        try:
            ends_in = max(0, int((
                datetime.fromisoformat(pred["next_period_at"])
                - datetime.fromisoformat(pred["server_time"])
            ).total_seconds()))
        except Exception:
            ends_in = "—"
        segments += [
            (f"Active Period ({timer}): ", "bold"),
            (f"…{current_period[-6:] if len(current_period) >= 6 else current_period}", "code"),
            (f"  ·  ⏳ {ends_in}s left\n", None),
        ]
        if sig:
            segments += [
                ("Live Signal: ", "bold"),
                (f"{str(sig.get('size', '')).upper()} · {str(sig.get('colour', '')).upper()}", "code"),
            ]
            if sig.get("confidence"):
                segments.append((f" ({sig['confidence']})", None))
            segments.append(("\n", None))
            if sig.get("reason"):
                segments.append((f"{sig['reason']}\n", "italic"))
            # Record it for the accuracy tracker, exactly like the API
            # endpoint does (first signal wins per period; never raises).
            with _db_session() as db:
                record_signal(
                    db, timer, pred["period"], sig["size"],
                    sig.get("reason"), sig.get("confidence"),
                )

    if active_stats["total"] > 0:
        streak_str = (
            f"🔥 {active_stats['streak_count']}W Streak"
            if active_stats["streak_win"]
            else f"⚠️ {active_stats['streak_count']}L"
        )
        segments += [
            (f"\n📊 {timer.upper()} Performance:", "bold"),
            (f"\n• All-Time Win Rate: {active_stats['acc']}% ({active_stats['wins']}/{active_stats['total']})", None),
            (f"\n• Last 20 Form: [{_confidence_meter(active_stats['acc_20'])}] {active_stats['acc_20']}%", None),
            (f"\n• Current Streak: {streak_str}  ·  Best Streak: 🏆 {active_stats['best_win_streak']}W", None),
            (f"\n• Last 10 Tape: {active_stats['tape']}", None),
        ]
    else:
        segments.append((f"\n📊 {timer.upper()} Performance: Warming up settled signals…", None))

    other_timer = "30s" if timer == "1m" else "1m"
    other_stats = s30 if timer == "1m" else s1
    if other_stats["total"] > 0:
        segments.append(
            (f"\n\n⚡ {other_timer.upper()} Summary: {other_stats['acc']}% ({other_stats['wins']}/{other_stats['total']} settled)", None)
        )

    segments.append(("\n\n⚠️ Signals are statistical estimates — play responsibly, never a guarantee.", "italic"))
    text, entities = _render(segments)

    btn_rows = []
    predict_url = _predict_url()
    if predict_url:
        btn_rows.append([
            InlineKeyboardButton(
                "Open Full Predictions Mini App",
                web_app=WebAppInfo(url=predict_url),
                style="primary",
                icon_custom_emoji_id=EMOJI_DIAMOND,
            )
        ])
    btn_rows.append([
        InlineKeyboardButton(
            "⏱ 1m Active" if timer == "1m" else "⏱ Switch to 1m",
            callback_data="__signal__:1m",
            style="success" if timer == "1m" else None,
        ),
        InlineKeyboardButton(
            "⚡ 30s Active" if timer == "30s" else "⚡ Switch to 30s",
            callback_data="__signal__:30s",
            style="success" if timer == "30s" else None,
        ),
    ])
    btn_rows.append([
        InlineKeyboardButton("🔄 Refresh Live Feed", callback_data=f"__signal__:{timer}", icon_custom_emoji_id=EMOJI_TARGET),
        InlineKeyboardButton("⬅️ Main Menu", callback_data="__home__"),
    ])
    return text, entities, InlineKeyboardMarkup(btn_rows)


def _admin_dashboard() -> tuple[str, list, InlineKeyboardMarkup]:
    """Live admin overview: counts by status, today's intake, approval rate, 7d trend, recent rows."""
    t0 = time.perf_counter()
    with _db_session() as db:
        rows = db.query(Submission).order_by(desc(Submission.submitted_at)).limit(5).all()
        total = db.query(Submission).count()
        total_users = db.query(User).count()
        counts = {s: 0 for s in STATUSES}
        for st, n in db.query(Submission.status, func.count()).group_by(Submission.status).all():
            counts[st] = n
        now_utc = datetime.now(timezone.utc)
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        today = db.query(Submission).filter(Submission.submitted_at >= today_start).count()
        admin_count = db.query(Admin).count()
        owners = {}
        if rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()
            }
    ping_ms = round((time.perf_counter() - t0) * 1000, 1)

    decided = counts.get("verified", 0) + counts.get("rejected", 0)
    approval_rate = f"{round(counts.get('verified', 0) / decided * 100, 1)}%" if decided else "—"
    active_wait = counts.get("pending_review", 0) + counts.get("under_review", 0)

    segments = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("Admin Control Center", "bold"),
        ("\n\n", None),
        (f"Users: {total_users}  ·  Submissions: {total}  ·  Today: +{today}", None),
        ("\n", None),
        (f"Queue waiting: {active_wait} (Pending: {counts['pending_review']} · Review: {counts['under_review']})", None),
        ("\n", None),
        (f"Verified: {counts['verified']} ({approval_rate})  ·  Rejected: {counts['rejected']}  ·  Info: {counts['info_required']}", None),
        ("\n", None),
        (f"Admins: {len(settings.admin_telegram_ids) + admin_count}  ·  DB Ping: {ping_ms}ms  ·  Up: {_format_uptime()}", None),
    ]
    if rows:
        segments.append(("\n\nRecent submissions:", "bold"))
        for r in rows:
            when = r.submitted_at.strftime("%m-%d %H:%M") if r.submitted_at else "—"
            icon = STATUS_ICONS.get(r.status, "•")
            segments.append((
                f"\n{icon} #{r.id} · {r.registration_uid} · {STATUS_LABELS.get(r.status, r.status)}"
                f" · user {owners.get(r.user_id, '?')} · {when}",
                None,
            ))
    else:
        segments.append(("\n\nNo submissions yet.", None))
    text, entities = _render(segments)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📋 Pending ({active_wait})", callback_data="admin:pending"),
            InlineKeyboardButton("🔍 Search UID/User", callback_data="admin:search"),
        ],
        [
            InlineKeyboardButton("📈 Deep Analytics", callback_data="admin:analytics"),
            InlineKeyboardButton("📣 Broadcast Studio", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton("👥 Manage Admins", callback_data="admin:admins"),
            InlineKeyboardButton("📥 Export CSV", callback_data="admin:export"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Center", callback_data="admin:refresh:d"),
        ],
    ])
    return text, entities, keyboard


def _admin_analytics_view() -> tuple[str, list, InlineKeyboardMarkup]:
    """Deep analytics screen: 7-day intake sparkline, conversion funnel, turnaround speed & signal health."""
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = today_start - timedelta(days=6)

    with _db_session() as db:
        total_users = db.query(User).count()
        users_with_subs = db.query(func.count(func.distinct(Submission.user_id))).scalar() or 0
        verified_users = (
            db.query(func.count(func.distinct(Submission.user_id)))
            .filter(Submission.status == "verified")
            .scalar()
            or 0
        )
        recent_subs = (
            db.query(Submission)
            .filter(Submission.submitted_at >= seven_days_ago)
            .all()
        )
        decided_subs = (
            db.query(Submission)
            .filter(Submission.status.in_(("verified", "rejected", "info_required")))
            .order_by(desc(Submission.submitted_at))
            .limit(200)
            .all()
        )
        sig_1m = db.query(SignalRecord).filter(SignalRecord.timer == "1m").all()
        sig_30s = db.query(SignalRecord).filter(SignalRecord.timer == "30s").all()

    # 7-day daily buckets
    daily_counts = [0] * 7
    day_labels = []
    for i in range(7):
        d = (seven_days_ago + timedelta(days=i)).date()
        day_labels.append(d.strftime("%a"))
        for s in recent_subs:
            if s.submitted_at and s.submitted_at.date() == d:
                daily_counts[i] += 1

    spark = _sparkline(daily_counts)

    # Average review turnaround time
    durations_min = []
    for s in decided_subs:
        if s.submitted_at and getattr(s, "updated_at", None):
            sub_t = s.submitted_at if s.submitted_at.tzinfo else s.submitted_at.replace(tzinfo=timezone.utc)
            upd_t = s.updated_at if s.updated_at.tzinfo else s.updated_at.replace(tzinfo=timezone.utc)
            diff_m = (upd_t - sub_t).total_seconds() / 60.0
            if 0 < diff_m < 60 * 24 * 14:
                durations_min.append(diff_m)
    avg_turnaround = (
        f"{round(sum(durations_min) / len(durations_min), 1)} min"
        if durations_min
        else "—"
    )

    sub_rate = round(users_with_subs / total_users * 100, 1) if total_users else 0.0
    ver_rate = round(verified_users / total_users * 100, 1) if total_users else 0.0
    s1 = _signal_stats_summary(sig_1m)
    s30 = _signal_stats_summary(sig_30s)

    segments = [
        ("📈", ("custom", EMOJI_CHART_UP)),
        (" ", None),
        ("VerifyHub — Deep Analytics & Funnel", "bold"),
        ("\n\nConversion Funnel:", "bold"),
        (f"\n• Total Bot Users: {total_users}", None),
        (f"\n• Submitted UID: {users_with_subs} ({sub_rate}% of users)", None),
        (f"\n• Verified Users: {verified_users} ({ver_rate}% of users)", None),
        (f"\n• Avg Review Speed: ⚡ {avg_turnaround}", None),
        ("\n\n7-Day Submission Trend:", "bold"),
        (f"\n{spark}  ({sum(daily_counts)} total in 7d)", "code"),
        (f"\n{' · '.join(f'{lbl}:{cnt}' for lbl, cnt in zip(day_labels, daily_counts))}", None),
        ("\n\nSignal Engine Health:", "bold"),
        (f"\n• 1m WinGo: {s1['acc']}% ({s1['wins']}/{s1['total']}) · Last 10: {s1['tape']}", None),
        (f"\n• 30s WinGo: {s30['acc']}% ({s30['wins']}/{s30['total']}) · Last 10: {s30['tape']}", None),
        (f"\n\nRuntime Uptime: {_format_uptime()}", "italic"),
    ]
    text, entities = _render(segments)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Submissions CSV", callback_data="admin:export"),
            InlineKeyboardButton("📥 Users CSV", callback_data="admin:export_users"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Analytics", callback_data="admin:analytics"),
            InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin:dashboard"),
        ],
    ])
    return text, entities, kb


def _admins_view() -> tuple[str, InlineKeyboardMarkup]:
    """Every admin (server config first, then bot-added) with remove buttons."""
    with _db_session() as db:
        rows = list_db_admins(db)
    lines = ["👥 Platform Admins:"]
    keyboard_rows = []
    for tid in settings.admin_telegram_ids:
        lines.append(f"• {tid} (server config)")
    for r in rows:
        lines.append(f"• {r.telegram_id} (added by {r.added_by_telegram_id})")
        keyboard_rows.append([
            InlineKeyboardButton(f"❌ Remove {r.telegram_id}", callback_data=f"admin:remove:{r.telegram_id}")
        ])
    keyboard_rows.append([InlineKeyboardButton("➕ Add Admin", callback_data="admin:add")])
    keyboard_rows.append([InlineKeyboardButton("⬅️ Back to Panel", callback_data="admin:dashboard")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def _pending_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """The review queue as text plus one-tap action buttons per row, pagination & bulk approve."""
    page = max(0, page)
    with _db_session() as db:
        total_pending = (
            db.query(Submission)
            .filter(Submission.status.in_(ACTIVE_STATUSES))
            .count()
        )
        total_pages = max(1, math.ceil(total_pending / PENDING_PAGE_SIZE))
        if page >= total_pages:
            page = total_pages - 1
        rows = (
            db.query(Submission)
            .filter(Submission.status.in_(ACTIVE_STATUSES))
            .order_by(desc(Submission.submitted_at))
            .offset(page * PENDING_PAGE_SIZE)
            .limit(PENDING_PAGE_SIZE)
            .all()
        )
        owners = {}
        if rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()
            }

    nav = [[
        InlineKeyboardButton("📊 Admin Panel", callback_data="admin:dashboard"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"admin:pending:{page}" if page else "admin:refresh:p"),
    ]]
    if not rows:
        return "✅ No submissions are waiting for review.", InlineKeyboardMarkup(nav)

    lines = [f"📋 Pending submissions ({total_pending} total · page {page + 1}/{total_pages}):"]
    keyboard_rows = []
    for r in rows:
        when = r.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if r.submitted_at else "—"
        icon = STATUS_ICONS.get(r.status, "⏳")
        lines.append(
            f"{icon} #{r.id} · {r.registration_uid} · {STATUS_LABELS.get(r.status, r.status)}"
            f" · user {owners.get(r.user_id, '?')} · {when}"
        )
        keyboard_rows.append([
            InlineKeyboardButton(f"✅ #{r.id}", callback_data=f"admin:approve:{r.id}:p"),
            InlineKeyboardButton(f"❌ #{r.id}", callback_data=f"admin:reject:{r.id}:p"),
            InlineKeyboardButton(f"ℹ️ #{r.id}", callback_data=f"admin:info:{r.id}:p"),
            InlineKeyboardButton(f"🔎 #{r.id}", callback_data=f"admin:view:{r.id}"),
        ])

    if len(rows) > 1:
        keyboard_rows.append([
            InlineKeyboardButton(
                f"⚡ Bulk Approve Page ({len(rows)} items)",
                callback_data=f"admin:bulk_ask:{page}",
            )
        ])

    if total_pages > 1:
        pager = []
        if page > 0:
            pager.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:pending:{page - 1}"))
        pager.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="admin:pending"))
        if page + 1 < total_pages:
            pager.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:pending:{page + 1}"))
        keyboard_rows.append(pager)

    lines.append("\nTap ✅ to approve, ❌ to reject, ℹ️ for info, or 🔎 for preset reasons & custom notes.")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows + nav)


def _submission_detail_view(submission_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Detailed inspector for a single submission with 1-tap presets, custom note & direct user reply."""
    with _db_session() as db:
        row = db.query(Submission).filter(Submission.id == submission_id).first()
        if row is None:
            return (
                f"Submission #{submission_id} not found.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Panel", callback_data="admin:dashboard")]]),
            )
        owner = db.query(User).filter(User.id == row.user_id).first()
        owner_tid = owner.telegram_id if owner else "?"
        user_subs = (
            db.query(Submission)
            .filter(Submission.user_id == row.user_id)
            .order_by(desc(Submission.submitted_at))
            .limit(5)
            .all()
        )

    when = row.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if row.submitted_at else "—"
    updated = (
        row.updated_at.strftime("%Y-%m-%d %H:%M UTC")
        if getattr(row, "updated_at", None)
        else "—"
    )
    icon = STATUS_ICONS.get(row.status, "•")
    lines = [
        f"🔎 Submission Inspector #{row.id}",
        "",
        f"UID: {row.registration_uid}",
        f"Status: {icon} {STATUS_LABELS.get(row.status, row.status)}",
        f"User Telegram ID: {owner_tid} ({len(user_subs)} recent submissions)",
        f"Submitted: {when}",
        f"Last Updated: {updated}",
    ]
    if row.review_message:
        lines.append(f"Review Note: {row.review_message}")
    if len(user_subs) > 1:
        lines.append("\nUser's Submission History:")
        for s in user_subs:
            s_icon = STATUS_ICONS.get(s.status, "•")
            s_when = s.submitted_at.strftime("%m-%d %H:%M") if s.submitted_at else "—"
            lines.append(f"  {s_icon} #{s.id} · {s.registration_uid} · {s_when}")

    kb_rows = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{row.id}:p"),
            InlineKeyboardButton("🔎 Mark Under Review", callback_data=f"admin:review:{row.id}:p"),
        ],
        [
            InlineKeyboardButton(
                REJECT_PRESETS["uid_not_found"][0],
                callback_data=f"admin:preset:r:uid_not_found:{row.id}",
            ),
            InlineKeyboardButton(
                REJECT_PRESETS["wrong_link"][0],
                callback_data=f"admin:preset:r:wrong_link:{row.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                REJECT_PRESETS["duplicate"][0],
                callback_data=f"admin:preset:r:duplicate:{row.id}",
            ),
            InlineKeyboardButton(
                INFO_PRESETS["deposit"][0],
                callback_data=f"admin:preset:i:deposit:{row.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                INFO_PRESETS["recheck_uid"][0],
                callback_data=f"admin:preset:i:recheck_uid:{row.id}",
            ),
            InlineKeyboardButton(
                INFO_PRESETS["activity"][0],
                callback_data=f"admin:preset:i:activity:{row.id}",
            ),
        ],
        [
            InlineKeyboardButton("✍️ Reject + Custom Note", callback_data=f"admin:note:reject:{row.id}:p"),
            InlineKeyboardButton("✍️ Info + Custom Note", callback_data=f"admin:note:info:{row.id}:p"),
        ],
    ]
    if isinstance(owner_tid, int):
        kb_rows.append([
            InlineKeyboardButton(f"↩️ Send Direct Message to {owner_tid}", callback_data=f"admin:reply:{owner_tid}"),
        ])
    kb_rows.append([
        InlineKeyboardButton("📋 Pending Queue", callback_data="admin:pending"),
        InlineKeyboardButton("📊 Admin Panel", callback_data="admin:dashboard"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


def _search_submissions(query_str: str) -> tuple[str, InlineKeyboardMarkup]:
    """Search by Submission ID (#12), Telegram ID, or Registration UID."""
    q = query_str.strip().lstrip("#")
    with _db_session() as db:
        matched_rows: list[Submission] = []
        if q.isdigit():
            num = int(q)
            by_id = db.query(Submission).filter(Submission.id == num).first()
            if by_id:
                matched_rows.append(by_id)
            user = db.query(User).filter(User.telegram_id == num).first()
            if user:
                user_rows = (
                    db.query(Submission)
                    .filter(Submission.user_id == user.id)
                    .order_by(desc(Submission.submitted_at))
                    .limit(8)
                    .all()
                )
                for r in user_rows:
                    if all(x.id != r.id for x in matched_rows):
                        matched_rows.append(r)
        uid_rows = (
            db.query(Submission)
            .filter(Submission.registration_uid.ilike(f"%{query_str.strip()}%"))
            .order_by(desc(Submission.submitted_at))
            .limit(8)
            .all()
        )
        for r in uid_rows:
            if all(x.id != r.id for x in matched_rows):
                matched_rows.append(r)

        owners = {}
        if matched_rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in matched_rows])).all()
            }

    if not matched_rows:
        return (
            f"No submissions or users matched '{query_str}'.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="admin:search")],
                [InlineKeyboardButton("⬅️ Back to Panel", callback_data="admin:dashboard")],
            ]),
        )

    lines = [f"🔍 Search results for '{query_str}' ({len(matched_rows)} found):"]
    kb_rows = []
    for r in matched_rows[:6]:
        when = r.submitted_at.strftime("%Y-%m-%d %H:%M") if r.submitted_at else "—"
        icon = STATUS_ICONS.get(r.status, "•")
        lines.append(
            f"{icon} #{r.id} · UID {r.registration_uid} · user {owners.get(r.user_id, '?')} · {when}"
        )
        kb_rows.append([
            InlineKeyboardButton(f"✅ #{r.id}", callback_data=f"admin:approve:{r.id}:p"),
            InlineKeyboardButton(f"❌ #{r.id}", callback_data=f"admin:reject:{r.id}:p"),
            InlineKeyboardButton(f"ℹ️ #{r.id}", callback_data=f"admin:info:{r.id}:p"),
            InlineKeyboardButton(f"🔎 #{r.id}", callback_data=f"admin:view:{r.id}"),
        ])
    kb_rows.append([
        InlineKeyboardButton("🔍 New Search", callback_data="admin:search"),
        InlineKeyboardButton("📊 Admin Panel", callback_data="admin:dashboard"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


def _export_submissions_csv() -> tuple[bytes, int]:
    """Generate a UTF-8 CSV dump of up to 2,000 recent submissions for admin audit."""
    with _db_session() as db:
        rows = db.query(Submission).order_by(desc(Submission.submitted_at)).limit(2000).all()
        owners = {}
        if rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()
            }
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "submission_id",
            "user_id",
            "telegram_id",
            "registration_uid",
            "status",
            "submitted_at_utc",
            "updated_at_utc",
            "review_message",
        ])
        for r in rows:
            writer.writerow([
                r.id,
                r.user_id,
                owners.get(r.user_id, ""),
                r.registration_uid,
                r.status,
                r.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if r.submitted_at else "",
                r.updated_at.strftime("%Y-%m-%d %H:%M:%S") if getattr(r, "updated_at", None) else "",
                r.review_message or "",
            ])
        return buf.getvalue().encode("utf-8"), len(rows)


def _export_users_csv() -> tuple[bytes, int]:
    """Generate a UTF-8 CSV dump of users and their submission counts/statuses."""
    with _db_session() as db:
        users = db.query(User).order_by(desc(User.id)).limit(2000).all()
        subs = db.query(Submission).all()
        subs_by_user: dict[int, list[Submission]] = {}
        for s in subs:
            subs_by_user.setdefault(s.user_id, []).append(s)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "user_id",
            "telegram_id",
            "first_seen_at_utc",
            "total_submissions",
            "is_verified",
            "latest_uid",
            "latest_status",
        ])
        for u in users:
            u_subs = sorted(
                subs_by_user.get(u.id, []),
                key=lambda x: x.submitted_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            latest = u_subs[0] if u_subs else None
            is_ver = any(x.status == "verified" for x in u_subs)
            writer.writerow([
                u.id,
                u.telegram_id,
                u.first_seen_at.strftime("%Y-%m-%d %H:%M:%S") if getattr(u, "first_seen_at", None) else "",
                len(u_subs),
                "yes" if is_ver else "no",
                latest.registration_uid if latest else "",
                latest.status if latest else "not_submitted",
            ])
        return buf.getvalue().encode("utf-8"), len(users)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if _too_fast(user_id):
        return
    context.user_data.pop("awaiting", None)
    admin = _is_admin(user_id)
    verified = _is_user_verified(user_id)
    text, entities = _welcome_text(
        first_name=update.effective_user.first_name,
        verified=verified,
        lang=_get_lang(user_id),
    )
    if not settings.mini_app_url and not settings.registration_url:
        text += "\n\nThe panel and registration links are not configured yet — ask the administrator to set MINI_APP_URL and REGISTRATION_URL."
    await update.message.reply_text(
        text,
        entities=entities,
        reply_markup=_keyboard(admin=admin, user_id=user_id),
    )
    if admin:
        # An admin opening the bot sees the whole picture right away:
        # the Admin Control Center is the ONE admin home — every admin
        # tool lives there, so there is no second (reply-keyboard) copy
        # of the same actions with different labels.
        dtext, dentities, dkeyboard = _admin_dashboard()
        await update.message.reply_text(dtext, entities=dentities, reply_markup=dkeyboard)


def _stats_text(telegram_id: int) -> tuple[str, list]:
    """Public live stats: real platform numbers + 1m/30s signal streaks + caller's VIP tier & badges."""
    with _db_session() as db:
        total_users = db.query(User).count()
        total_subs = db.query(Submission).count()
        verified = db.query(Submission).filter(Submission.status == "verified").count()
        pending = db.query(Submission).filter(Submission.status.in_(ACTIVE_STATUSES)).count()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        verified_today = db.query(Submission).filter(
            Submission.status == "verified", Submission.updated_at >= today_start
        ).count()
        acc_rows_1m = db.query(SignalRecord).filter(SignalRecord.timer == "1m").all()
        acc_rows_30s = db.query(SignalRecord).filter(SignalRecord.timer == "30s").all()

    s1 = _signal_stats_summary(acc_rows_1m)
    s30 = _signal_stats_summary(acc_rows_30s)
    badges = _badges_for(telegram_id)
    tier = _vip_tier(badges, "✅ Verified" in badges, 1 if badges else 0)

    segments = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("VerifyHub — Live Platform Telemetry", "bold"),
        ("\n\n", None),
        (f"Users: {total_users}  ·  Submissions: {total_subs}", None),
        ("\n", None),
        (f"Verified: {verified}  ·  Verified today: +{verified_today}  ·  Queue: {pending}", None),
    ]
    if s1["total"]:
        segments.append((
            f"\n\n🎯 Signals (1m): {s1['acc']}% ({s1['wins']}/{s1['total']}) · Best Streak: {s1['best_win_streak']}W"
            f"\nRecent 1m Tape: {s1['tape']}",
            None,
        ))
    else:
        segments.append(("\n\n🎯 Signals (1m): no settled signals yet.", None))
    if s30["total"]:
        segments.append((
            f"\n⚡ Signals (30s): {s30['acc']}% ({s30['wins']}/{s30['total']}) · Best Streak: {s30['best_win_streak']}W",
            None,
        ))
    try:
        live = get_live()
        segments.append((f"\n\nLive WinGo 1m: period …{live['period'][-4:]} ends in {live['ends_in_seconds']}s", None))
    except Exception:
        logger.debug("Live period unavailable for stats.")
    segments += [
        ("\n\nYour Tier: ", "bold"),
        (tier, None),
    ]
    if badges:
        segments.append(("\nYour Badges: ", "bold"))
        segments.append((" · ".join(badges), None))
    else:
        segments.append(("\nNo badges yet — get verified to earn your first one.", None))
    return _render(segments)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text replies to interactive bot prompts (submit UID, support
    message, admin reply, search, custom note, broadcast, add admin).

    The bot is button-only: there is no reply keyboard, so stray text
    just gets the nudge below — every action lives on an inline button.
    """
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    admin = _is_admin(user_id)  # one DB check per message, reused below

    awaiting = context.user_data.get("awaiting")

    # 1. User In-Chat Quick UID Submission
    if awaiting == "submit_uid":
        res_text, res_entities, res_kb, sub = await _submit_uid_in_chat(update.effective_user, text)
        if sub is not None:
            context.user_data.pop("awaiting", None)
        await update.message.reply_text(res_text, entities=res_entities, reply_markup=res_kb)
        return

    # 2. User Support / Appeal Message to Admins
    if awaiting == "support_msg":
        context.user_data.pop("awaiting", None)
        if len(text) < 4:
            await update.message.reply_text(
                "Please write a clear message (at least a few words) so an admin can help you.",
                reply_markup=_keyboard(admin=admin, user_id=user_id),
            )
            return
        status, latest_row, _ = _latest_status(user_id)
        who = f"@{update.effective_user.username}" if update.effective_user.username else (update.effective_user.first_name or str(user_id))
        uid_info = latest_row.registration_uid if latest_row else "None"
        support_alert = (
            f"📩 Support / Appeal Ticket\n\n"
            f"From: {who} ({user_id})\n"
            f"Latest UID: {uid_info} ({STATUS_LABELS.get(status, status)})\n\n"
            f"Message:\n{text}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("↩️ Reply to User", callback_data=f"admin:reply:{user_id}"),
                InlineKeyboardButton("🔍 Inspect User", callback_data=f"admin:finduser:{user_id}"),
            ]
        ])
        reached = 0
        for aid in _all_admin_ids():
            try:
                await context.bot.send_message(aid, support_alert, reply_markup=kb)
                reached += 1
            except Exception:
                pass
        reply_text, reply_entities = _render([
            ("✅", ("custom", EMOJI_CHECK)),
            (" Your message has been sent to the administrators. You will receive their reply right here in chat.", None),
        ])
        await update.message.reply_text(
            reply_text,
            entities=reply_entities,
            reply_markup=_keyboard(admin=admin, user_id=user_id),
        )
        return

    # 3. Admin Direct Reply to a User
    if isinstance(awaiting, str) and awaiting.startswith("reply_user:") and admin:
        target_tid_str = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        try:
            target_tid = int(target_tid_str)
        except ValueError:
            return
        try:
            msg_text, msg_entities = _render([
                ("👑", ("custom", EMOJI_CROWN)),
                (" ", None),
                ("Message from VerifyHub Admin:", "bold"),
                (f"\n\n{text}", None),
            ])
            await context.bot.send_message(
                target_tid,
                msg_text,
                entities=msg_entities,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("↩️ Reply to Admin", callback_data="__support__"),
                        InlineKeyboardButton("📊 Check My Status", callback_data="__status__"),
                    ]
                ]),
            )
            await update.message.reply_text(
                f"✅ Sent message to user {target_tid}.",
                reply_markup=_admin_dashboard()[2],
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Could not message user {target_tid}: {e}",
                reply_markup=_admin_dashboard()[2],
            )
        return

    # 4. Admin Search UID / User
    if awaiting == "search_uid" and admin:
        context.user_data.pop("awaiting", None)
        stext, skeyboard = _search_submissions(text)
        await update.message.reply_text(stext, reply_markup=skeyboard)
        return

    # 5. Admin Custom Review Note
    if isinstance(awaiting, str) and awaiting.startswith("custom_note:") and admin:
        _, action_key, sid_str, origin = awaiting.split(":", 3)
        context.user_data.pop("awaiting", None)
        try:
            sid = int(sid_str)
        except ValueError:
            return
        to_status = "rejected" if action_key == "reject" else "info_required"
        res_text, res_entities, error = await _do_admin_action(user_id, sid, to_status, text)
        if error is not None:
            await update.message.reply_text(error, reply_markup=_keyboard(admin=True, user_id=user_id))
            return
        ptext, pkeyboard = _pending_view()
        await update.message.reply_text(
            res_text + "\n\n" + ptext,
            entities=res_entities,
            reply_markup=pkeyboard if origin == "p" else _admin_dashboard()[2],
        )
        return

    # 6. Admin Broadcast Studio Preview
    if awaiting == "broadcast" and admin:
        context.user_data.pop("awaiting", None)
        context.user_data["broadcast_text"] = text
        context.user_data["broadcast_entities"] = list(update.message.entities or [])
        context.user_data["broadcast_pin"] = False
        total, verified = _broadcast_counts()
        pending_cnt = len(_broadcast_targets("pending"))
        rejected_cnt = len(_broadcast_targets("rejected"))
        preview = (
            f"📣 Broadcast Studio Preview:\n\n{text}\n\n"
            f"All: {total}  ·  Verified: {verified}  ·  Pending: {pending_cnt}  ·  Rejected/Info: {rejected_cnt}"
        )
        await update.message.reply_text(
            preview,
            reply_markup=_broadcast_preview_keyboard(total, verified, pending_cnt, rejected_cnt, pin=False),
        )
        return

    # 7. Admin Add New Admin
    if awaiting == "add_admin" and admin:
        if not text.isdigit():
            await update.message.reply_text(
                "That doesn't look like a Telegram ID — send just the number.",
                reply_markup=_keyboard(admin=True, user_id=user_id),
            )
            return
        new_id = int(text)
        if settings.is_admin(new_id):
            await update.message.reply_text(
                "That user is already an admin (set in the server config).",
                reply_markup=_keyboard(admin=True, user_id=user_id),
            )
            return
        with _db_session() as db:
            ok, error = add_admin(db, telegram_id=new_id, added_by=user_id)
        if not ok:
            await update.message.reply_text(error, reply_markup=_keyboard(admin=True, user_id=user_id))
            return
        context.user_data.pop("awaiting", None)
        reply_text, entities = _render([
            ("✅", ("custom", EMOJI_CHECK)),
            (f" {new_id} is now an admin. They get the Admin Control Center and full admin access.", None),
        ])
        await update.message.reply_text(reply_text, entities=entities, reply_markup=_keyboard(admin=True, user_id=user_id))
        return

    await update.message.reply_text(
        "Use the buttons below — tap /start if they are not showing.",
        reply_markup=_keyboard(admin=admin, user_id=user_id),
    )


def _broadcast_preview_keyboard(
    total: int,
    verified: int,
    pending_cnt: int,
    rejected_cnt: int,
    pin: bool = False,
) -> InlineKeyboardMarkup:
    pin_label = "📌 Auto-Pin: ON ✅" if pin else "📌 Auto-Pin: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pin_label, callback_data="admin:broadcast:toggle_pin")],
        [InlineKeyboardButton(f"📣 Send to All ({total})", callback_data="admin:broadcast:all")],
        [InlineKeyboardButton(f"✅ Send to Verified Only ({verified})", callback_data="admin:broadcast:verified")],
        [
            InlineKeyboardButton(f"⏳ Unverified ({pending_cnt})", callback_data="admin:broadcast:pending"),
            InlineKeyboardButton(f"❌ Rejected/Info ({rejected_cnt})", callback_data="admin:broadcast:rejected"),
        ],
        [InlineKeyboardButton("✖️ Cancel", callback_data="admin:cancel-broadcast")],
    ])


async def _do_admin_action(admin_id: int, submission_id: int, to_status: str, message: str | None):
    """Run the shared transition + user notification.

    Returns ``(text, entities, error)``: on success the confirmation to show
    the admin; on failure ``(None, None, human-readable reason)``.
    """
    with _db_session() as db:
        row, error, _not_found = change_submission_status(
            db,
            submission_id=submission_id,
            to_status=to_status,
            message=message,
            admin_telegram_id=admin_id,
        )
        owner_telegram_id = None
        if row is not None:
            owner = db.query(User).filter(User.id == row.user_id).first()
            owner_telegram_id = owner.telegram_id if owner else None
    if error is not None:
        return None, None, error
    notified = False
    if owner_telegram_id is not None:
        notified = await notify_user(
            settings.bot_token, owner_telegram_id, to_status, row.review_message, settings.mini_app_url
        )
    verb = {
        "verified": "verified",
        "rejected": "rejected",
        "info_required": "marked as needing more information",
        "under_review": "marked as under review",
    }.get(to_status, to_status)
    text, entities = _render([
        ("✅", ("custom", EMOJI_CHECK)),
        (f" Submission #{row.id} {verb}.", None),
    ])
    if row.review_message:
        text += f"\nReview message: {row.review_message}"
    text += (
        "\nThe user has been notified."
        if notified
        else "\nThe user could not be notified (they may have blocked the bot)."
    )
    return text, entities, None


async def _button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer()
    admin_id = query.from_user.id
    if _too_fast(admin_id):
        return
    admin = _is_admin(admin_id)
    data = query.data or ""

    if data == "__home__":
        context.user_data.pop("awaiting", None)
        text, entities = _welcome_text(
            first_name=query.from_user.first_name,
            verified=_is_user_verified(admin_id),
            lang=_get_lang(admin_id),
        )
        await _edit_or_reply(query, text, entities=entities, reply_markup=_keyboard(admin=admin, user_id=admin_id))

    elif data == "__lang__":
        new_lang = _toggle_lang(admin_id)
        text, entities = _welcome_text(
            first_name=query.from_user.first_name,
            verified=_is_user_verified(admin_id),
            lang=new_lang,
        )
        await _edit_or_reply(query, text, entities=entities, reply_markup=_keyboard(admin=admin, user_id=admin_id))

    elif data == "__submit_uid__":
        context.user_data["awaiting"] = "submit_uid"
        segments = [
            ("🚀 ", None),
            ("Quick UID Submission", "bold"),
            ("\n\nSend your Registration UID right now as a message (6–64 characters, letters, numbers, dash or underscore).", None),
            ("\n\nTip: Copy your exact UID from your account profile page on the official site.", "italic"),
        ]
        text, entities = _render(segments)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✖️ Cancel", callback_data="__home__")],
        ])
        await _edit_or_reply(query, text, entities=entities, reply_markup=kb)

    elif data == "__profile__":
        context.user_data.pop("awaiting", None)
        text, entities, prof_kb = _profile_card(
            admin_id,
            first_name=query.from_user.first_name,
            admin=admin,
        )
        await _edit_or_reply(query, text, entities=entities, reply_markup=prof_kb)

    elif data == "__support__":
        context.user_data["awaiting"] = "support_msg"
        segments = [
            ("💬 ", None),
            ("Contact VerifyHub Admin / Appeal", "bold"),
            ("\n\nType your question, UID issue, or appeal below and send it. An administrator will reply to you directly in this chat.", None),
        ]
        text, entities = _render(segments)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="__home__")]])
        await _edit_or_reply(query, text, entities=entities, reply_markup=kb)

    elif data == "__help__":
        text, entities = _help_text(admin=admin, lang=_get_lang(admin_id))
        # Help is a leaf screen: its own actions + Back. It used to
        # repeat the whole main menu underneath, which pushed the real
        # buttons down and made every screen look different.
        help_rows = [
            [
                InlineKeyboardButton("📊 Live Stats", callback_data="__stats__", icon_custom_emoji_id=EMOJI_CHART),
                InlineKeyboardButton("💬 Contact Admin", callback_data="__support__"),
            ],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="__home__")],
        ]
        await _edit_or_reply(query, text, entities=entities, reply_markup=InlineKeyboardMarkup(help_rows))

    elif data == "__stats__":
        text, entities = _stats_text(admin_id)
        stats_rows = [
            [InlineKeyboardButton("🔄 Refresh Stats", callback_data="__stats__", icon_custom_emoji_id=EMOJI_CHART)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="__home__")],
        ]
        await _edit_or_reply(query, text, entities=entities, reply_markup=InlineKeyboardMarkup(stats_rows))

    elif data == "__signal__" or data.startswith("__signal__:"):
        timer = "1m"
        if ":" in data:
            timer = data.split(":", 1)[1]
        text, entities, sig_kb = _signal_preview_card(admin_id, admin=admin, timer=timer)
        await _edit_or_reply(query, text, entities=entities, reply_markup=sig_kb)

    elif data == "__status__":
        status, row, _ = _latest_status(admin_id)
        history = _user_submissions(admin_id, limit=4)
        text, entities = _status_card(status, row, badges=_badges_for(admin_id), history=history)
        # Same leaf-screen rule as Help: own actions + Back, no repeated menu.
        status_kb_rows = [
            [
                InlineKeyboardButton("🔄 Refresh Status", callback_data="__status__", icon_custom_emoji_id=EMOJI_CHART),
                InlineKeyboardButton("🎯 Signal Hub", callback_data="__signal__:1m", icon_custom_emoji_id=EMOJI_TARGET),
            ],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="__home__")],
        ]
        await _edit_or_reply(query, text, entities=entities, reply_markup=InlineKeyboardMarkup(status_kb_rows))

    elif data == "__admin__" or data.startswith("admin:"):
        if not admin:
            if query.message:
                await query.message.reply_text("This command is for admins only.", reply_markup=_keyboard(user_id=admin_id))
            return

        if data == "__admin__" or data == "admin:dashboard":
            context.user_data.pop("awaiting", None)
            text, entities, keyboard = _admin_dashboard()
            if data == "__admin__" and query.message:
                await query.message.reply_text(text, entities=entities, reply_markup=keyboard)
            else:
                await _edit_or_reply(query, text, entities=entities, reply_markup=keyboard)
            return

        if data == "admin:analytics":
            context.user_data.pop("awaiting", None)
            text, entities, keyboard = _admin_analytics_view()
            await _edit_or_reply(query, text, entities=entities, reply_markup=keyboard)
            return

        if data == "admin:pending" or data.startswith("admin:pending:"):
            page = 0
            if ":" in data[len("admin:pending"):]:
                try:
                    page = int(data.split(":")[2])
                except (IndexError, ValueError):
                    page = 0
            text, keyboard = _pending_view(page=page)
            await _edit_or_reply(query, text, reply_markup=keyboard)
            return

        if data.startswith("admin:bulk_ask:"):
            try:
                page = int(data.split(":")[2])
            except (IndexError, ValueError):
                page = 0
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Approve All on Page", callback_data=f"admin:bulk_exec:{page}"),
                    InlineKeyboardButton("✖️ Cancel", callback_data=f"admin:pending:{page}"),
                ]
            ])
            await _edit_or_reply(
                query,
                f"⚡ Are you sure you want to approve all pending submissions on page {page + 1} and notify each user?",
                reply_markup=kb,
            )
            return

        if data.startswith("admin:bulk_exec:"):
            try:
                page = int(data.split(":")[2])
            except (IndexError, ValueError):
                page = 0
            with _db_session() as db:
                rows = (
                    db.query(Submission)
                    .filter(Submission.status.in_(ACTIVE_STATUSES))
                    .order_by(desc(Submission.submitted_at))
                    .offset(page * PENDING_PAGE_SIZE)
                    .limit(PENDING_PAGE_SIZE)
                    .all()
                )
                sids = [r.id for r in rows]
            approved_cnt = 0
            for sid in sids:
                _t, _e, err = await _do_admin_action(admin_id, sid, "verified", None)
                if err is None:
                    approved_cnt += 1
            ptext, pkeyboard = _pending_view(page=0)
            await _edit_or_reply(
                query,
                f"✅ Bulk-approved {approved_cnt} submissions and notified users.\n\n{ptext}",
                reply_markup=pkeyboard,
            )
            return

        if data in ("admin:refresh:d", "admin:refresh:p"):
            if data.endswith(":d"):
                text, entities, keyboard = _admin_dashboard()
                await _edit_or_reply(query, text, entities=entities, reply_markup=keyboard)
            else:
                text, keyboard = _pending_view()
                await _edit_or_reply(query, text, reply_markup=keyboard)
            return

        if data == "admin:search":
            context.user_data["awaiting"] = "search_uid"
            await _edit_or_reply(
                query,
                "Send a Registration UID, User Telegram ID, or #SubmissionID to search:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data="admin:dashboard"),
                ]]),
            )
            return

        if data.startswith("admin:finduser:"):
            q = data.split(":", 2)[2]
            stext, skeyboard = _search_submissions(q)
            await _edit_or_reply(query, stext, reply_markup=skeyboard)
            return

        if data.startswith("admin:reply:"):
            target_tid = data.split(":", 2)[2]
            context.user_data["awaiting"] = f"reply_user:{target_tid}"
            await _edit_or_reply(
                query,
                f"↩️ Type the message to send directly to User {target_tid}:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data="admin:dashboard"),
                ]]),
            )
            return

        if data == "admin:export":
            csv_bytes, count = _export_submissions_csv()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            if query.message:
                await query.message.reply_document(
                    document=InputFile(io.BytesIO(csv_bytes), filename=f"verifyhub_submissions_{stamp}.csv"),
                    caption=f"📥 Exported {count} submissions ({stamp} UTC).",
                )
            return

        if data == "admin:export_users":
            csv_bytes, count = _export_users_csv()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            if query.message:
                await query.message.reply_document(
                    document=InputFile(io.BytesIO(csv_bytes), filename=f"verifyhub_users_{stamp}.csv"),
                    caption=f"📥 Exported {count} users ({stamp} UTC).",
                )
            return

        if data == "admin:admins":
            text, keyboard = _admins_view()
            await _edit_or_reply(query, text, reply_markup=keyboard)
            return

        if data == "admin:add":
            context.user_data["awaiting"] = "add_admin"
            await _edit_or_reply(
                query,
                "Send the Telegram ID of the new admin (just the number).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data="admin:cancel-add"),
                ]]),
            )
            return

        if data == "admin:cancel-add":
            context.user_data.pop("awaiting", None)
            text, keyboard = _admins_view()
            await _edit_or_reply(query, text, reply_markup=keyboard)
            return

        if data == "admin:broadcast":
            context.user_data["awaiting"] = "broadcast"
            await _edit_or_reply(
                query,
                "Send the broadcast message (supports bold, italic, code & custom emoji).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data="admin:cancel-broadcast"),
                ]]),
            )
            return

        if data == "admin:broadcast:toggle_pin":
            text = context.user_data.get("broadcast_text")
            if not text:
                await _edit_or_reply(
                    query,
                    "Broadcast session expired — start again from the Admin Panel.",
                    reply_markup=_admin_dashboard()[2],
                )
                return
            pin = not bool(context.user_data.get("broadcast_pin", False))
            context.user_data["broadcast_pin"] = pin
            total, verified = _broadcast_counts()
            pending_cnt = len(_broadcast_targets("pending"))
            rejected_cnt = len(_broadcast_targets("rejected"))
            preview = (
                f"📣 Broadcast Studio Preview:\n\n{text}\n\n"
                f"All: {total}  ·  Verified: {verified}  ·  Pending: {pending_cnt}  ·  Rejected/Info: {rejected_cnt}"
            )
            await _edit_or_reply(
                query,
                preview,
                reply_markup=_broadcast_preview_keyboard(total, verified, pending_cnt, rejected_cnt, pin=pin),
            )
            return

        if data == "admin:cancel-broadcast":
            context.user_data.pop("awaiting", None)
            context.user_data.pop("broadcast_text", None)
            context.user_data.pop("broadcast_entities", None)
            context.user_data.pop("broadcast_pin", None)
            text, entities, keyboard = _admin_dashboard()
            await _edit_or_reply(query, text, entities=entities, reply_markup=keyboard)
            return

        if data in (
            "admin:broadcast:all",
            "admin:broadcast:verified",
            "admin:broadcast:pending",
            "admin:broadcast:rejected",
        ):
            text = context.user_data.get("broadcast_text")
            entities = context.user_data.get("broadcast_entities")
            pin = bool(context.user_data.get("broadcast_pin", False))
            if not text:
                if query.message:
                    await query.message.reply_text(
                        "Broadcast message not found — start again from the Admin Panel.",
                        reply_markup=_keyboard(admin=True, user_id=admin_id),
                    )
                return
            context.user_data.pop("broadcast_text", None)
            context.user_data.pop("broadcast_entities", None)
            context.user_data.pop("broadcast_pin", None)
            mode = data.split(":")[-1]
            targets = _broadcast_targets(mode)
            await _edit_or_reply(query, f"Sending broadcast to {len(targets)} users…")

            async def _progress(done: int, total: int, d: int, b: int, f: int) -> None:
                await _edit_or_reply(
                    query,
                    f"Sending broadcast… {done}/{total}\nDelivered: {d} · Blocked: {b} · Failed: {f}",
                )

            delivered, blocked, failed = await _send_broadcast(
                context.bot,
                text,
                targets,
                entities=entities,
                pin_message=pin,
                progress_cb=_progress,
            )
            await _edit_or_reply(
                query,
                f"Broadcast complete ({mode}).\nDelivered: {delivered}  ·  Blocked: {blocked}  ·  Failed: {failed}",
                reply_markup=_admin_dashboard()[2],
            )
            return

        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "view":
            try:
                sid = int(parts[2])
            except ValueError:
                return
            vtext, vkeyboard = _submission_detail_view(sid)
            await _edit_or_reply(query, vtext, reply_markup=vkeyboard)
            return

        if len(parts) == 5 and parts[1] == "preset" and parts[2] in ("r", "i"):
            kind = parts[2]
            preset_key = parts[3]
            try:
                sid = int(parts[4])
            except ValueError:
                return
            if kind == "r" and preset_key in REJECT_PRESETS:
                to_status = "rejected"
                msg = REJECT_PRESETS[preset_key][1]
            elif kind == "i" and preset_key in INFO_PRESETS:
                to_status = "info_required"
                msg = INFO_PRESETS[preset_key][1]
            else:
                return
            text, entities, error = await _do_admin_action(admin_id, sid, to_status, msg)
            if error is not None:
                if query.message:
                    await query.message.reply_text(error, reply_markup=_keyboard(admin=True, user_id=admin_id))
                return
            ptext, pkeyboard = _pending_view()
            await _edit_or_reply(
                query,
                text + "\n\n" + ptext,
                entities=entities,
                reply_markup=pkeyboard,
            )
            return

        if len(parts) == 5 and parts[1] == "note" and parts[2] in ("reject", "info"):
            action_key = parts[2]
            sid_str = parts[3]
            origin = parts[4]
            context.user_data["awaiting"] = f"custom_note:{action_key}:{sid_str}:{origin}"
            label = "rejection reason" if action_key == "reject" else "information request"
            await _edit_or_reply(
                query,
                f"✍️ Send the custom {label} for Submission #{sid_str}:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data=f"admin:view:{sid_str}"),
                ]]),
            )
            return

        if len(parts) == 3 and parts[1] == "remove":
            try:
                target = int(parts[2])
            except ValueError:
                return
            if target == admin_id:
                if query.message:
                    await query.message.reply_text("You cannot remove yourself.", reply_markup=_keyboard(admin=True, user_id=admin_id))
                return
            if settings.is_admin(target):
                if query.message:
                    await query.message.reply_text(
                        "That admin is set in the server config — remove them from ADMIN_TELEGRAM_IDS on Render.",
                        reply_markup=_keyboard(admin=True, user_id=admin_id),
                    )
                return
            with _db_session() as db:
                ok, error = remove_admin(db, telegram_id=target)
            if not ok:
                if query.message:
                    await query.message.reply_text(error, reply_markup=_keyboard(admin=True, user_id=admin_id))
                return
            text, entities = _render([
                ("✅", ("custom", EMOJI_CHECK)),
                (f" {target} is no longer an admin.", None),
            ])
            atext, akeyboard = _admins_view()
            await _edit_or_reply(
                query, text + "\n\n" + atext, entities=entities, reply_markup=akeyboard
            )
            return

        if len(parts) == 4 and parts[1] in ("approve", "reject", "info", "review"):
            try:
                sid = int(parts[2])
            except ValueError:
                return
            to_status = {
                "approve": "verified",
                "reject": "rejected",
                "info": "info_required",
                "review": "under_review",
            }[parts[1]]
            message = {
                "verified": None,
                "under_review": "An administrator is actively reviewing your UID submission.",
                "rejected": "Your submission was not approved by the administrator.",
                "info_required": "Additional information is required. Please check the verification panel.",
            }[to_status]
            text, entities, error = await _do_admin_action(admin_id, sid, to_status, message)
            if error is not None:
                if query.message:
                    await query.message.reply_text(error, reply_markup=_keyboard(admin=True, user_id=admin_id))
                return
            if parts[3] == "n":
                # The new-submission alert: replace it with the outcome.
                await _edit_or_reply(query, text, entities=entities)
            else:
                # The pending queue: confirmation on top, refreshed queue below.
                ptext, pkeyboard = _pending_view()
                await _edit_or_reply(
                    query, text + "\n\n" + ptext, entities=entities, reply_markup=pkeyboard
                )
            return


async def clear_bot_commands(bot) -> None:
    """Remove the Telegram command menu (default + per-admin scopes).

    The bot is button-only now: /start is the only entry point and every
    action lives on an inline keyboard. Runs at startup (post_init) so a
    stale menu left by an older deploy is cleared too. Never raises — a
    failed clear only means the menu is stale, and the bot keeps working.
    """
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        for admin_id in _all_admin_ids():
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception:
        logger.exception("Failed to clear bot command menu.")


async def _post_init(application: Application) -> None:
    await clear_bot_commands(application.bot)


def register_handlers(app: Application) -> None:
    """Register every VerifyHub handler on the given Application.

    Button-only by design: /start is the sole command (the entry point
    that shows the inline keyboard); every other action is a button tap
    (CallbackQueryHandler) or a plain-text reply to a bot prompt
    (MessageHandler, e.g. the add-admin ID).
    """
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_error_handler(_on_error)


def build_telegram_application(bot_token: str, *, with_updater: bool) -> Application:
    """Build a PTB Application with all VerifyHub handlers registered.

    ``with_updater=True`` is for the polling worker (``python -m app.bot``);
    ``with_updater=False`` is for the webhook endpoint, which feeds updates
    to the application directly and must not start a poller.
    """
    builder = Application.builder().token(bot_token)
    builder = builder.post_init(_post_init)
    if not with_updater:
        builder = builder.updater(None)
    app = builder.build()
    register_handlers(app)
    return app


# ---------------- Polling entry point ----------------
# `python -m app.bot` runs the bot with long polling (the paid Render
# Background Worker mode). On the free tier the bot instead runs via
# webhook inside the API service — same handlers, no worker needed.

def build_app():
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    return build_telegram_application(settings.bot_token, with_updater=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = build_app()
    logger.info("Starting VerifyHub bot (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
