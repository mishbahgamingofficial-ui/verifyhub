"""VerifyHub Telegram bot handlers — shared by polling and webhook modes.

All command / button behaviour lives here so the polling worker
(``app.bot``, ``python -m app.bot``) and the webhook endpoint in the API
(``POST /telegram/webhook``) run the *exact same* handlers. Nothing in
this module knows which transport delivered the update.

Handlers are registered with :func:`register_handlers`; use
:func:`build_telegram_application` to build an Application for either
mode (``with_updater=True`` for polling, ``False`` for webhook).
"""
from __future__ import annotations

import logging

from sqlalchemy import desc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .config import get_settings
from .db import Submission, User, get_session_factory, mask_uid
from .notifications import notify_user
from .services import ACTIVE_STATUSES, change_submission_status

logger = logging.getLogger("verifyhub.bot")
settings = get_settings()

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


def _welcome_text():
    return _render([
        ("🔥 ", None),
        ("VerifyHub — one pass, one verdict.", "bold"),
        ("\n\n", None),
        ("✅", ("custom", EMOJI_CHECK)),
        (" Send your UID once. An admin reviews it.", None),
        ("\n", None),
        ("🎯", ("custom", EMOJI_TARGET)),
        (" Verified users unlock WinGo predictions.", None),
        ("\n\nTap below to open the panel.", None),
    ])


def _help_text(admin: bool = False):
    segments = [
        ("👑", ("custom", EMOJI_CROWN)),
        (" ", None),
        ("VerifyHub — how it works", "bold"),
        ("\n\n", None),
        ("1.", "bold"),
        (" Register on the official site, copy your UID.", None),
        ("\n", None),
        ("2.", "bold"),
        (" Open the panel and send the UID once.", None),
        ("\n", None),
        ("3.", "bold"),
        (" An admin reviews it — watch the three windows.", None),
        ("\n\n", None),
        ("Verified", "bold"),
        (" = admin-approved. No guarantee of recovery, and not provider-confirmed.", None),
        ("\nVerified users unlock the Prediction tab (WinGo 30s + 1 min) — a signal, never a guarantee.", None),
        ("\n\nNo passwords, OTPs or recovery phrases are ever asked for.", None),
        ("\n\n", None),
        ("Commands", "bold"),
        ("\n/start — open the panel", None),
        ("\n/status — check your latest submission", None),
        ("\n/predict — open predictions (verified users)", None),
        ("\n/help — this guide", None),
        ("\n/cancel — cancel any pending operation", None),
    ]
    if admin:
        segments += [
            ("\n\n", None),
            ("Admin", "bold"),
            ("\n/pending — submissions waiting for review", None),
            ("\n/approve <id> [message] — mark verified", None),
            ("\n/reject <id> <message> — mark rejected", None),
            ("\n/needinfo <id> <message> — ask for more information", None),
        ]
    return _render(segments)

STATUS_LABELS = {
    "not_submitted": "Not submitted",
    "pending_review": "Pending review",
    "under_review": "Under review",
    "verified": "Verified (administrator-reviewed)",
    "rejected": "Rejected",
    "info_required": "Additional information required",
}


# Premium custom emojis for the bot buttons (Bot API 9.4 `icon_custom_emoji_id`).
# Resolved from the user's own sets via getStickerSet; the bot owner has
# Telegram Premium, which is what lets the bot use them. Older clients
# ignore the icon and show the plain label instead.
EMOJI_CROWN = "6052853159204757225"      # 👑 from emoji_H_M_by_TgEmodziBot
EMOJI_CHART = "5190806721286657692"      # 📊 from FinanceEmoji
EMOJI_DIAMOND = "6053338597883386982"   # 💎 from emoji_H_M_by_TgEmodziBot
EMOJI_THINKING = "6334471179801200139"  # 🧐 from peachgomaemoji
EMOJI_CHECK = "6055128392360010233"      # ✅ from emoji_H_M_by_TgEmodziBot
EMOJI_TARGET = "5310278924616356636"     # 🎯 from FinanceEmoji
EMOJI_CHART_UP = "5197503331215361533"   # 📈 from FinanceEmoji


def _keyboard() -> InlineKeyboardMarkup:
    """Main menu: stacked full-width buttons, coloured via Bot API 9.4 `style`.

    Only three styles exist (primary=blue, success=green, danger=red) and
    older Telegram clients ignore them, so the labels carry the meaning too
    (emoji + words) and at most two buttons per keyboard are styled.
    """
    rows = []
    if settings.mini_app_url:
        rows.append([InlineKeyboardButton("Open Verification Panel", web_app=WebAppInfo(url=settings.mini_app_url), style="primary", icon_custom_emoji_id=EMOJI_CROWN)])
    rows.append([InlineKeyboardButton("Check My Status", callback_data="__status__", style="success", icon_custom_emoji_id=EMOJI_CHART)])
    predict_url = _predict_url()
    if predict_url:
        rows.append([InlineKeyboardButton("Open Predictions", web_app=WebAppInfo(url=predict_url), icon_custom_emoji_id=EMOJI_DIAMOND)])
    rows.append([InlineKeyboardButton("Help", callback_data="__help__", icon_custom_emoji_id=EMOJI_THINKING)])
    return InlineKeyboardMarkup(rows)


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


def _status_card(status: str, row: Submission | None) -> tuple[str, list]:
    """Compact status card: badge word, masked UID, sent time, three windows.

    Returns (text, entities) — see _render. The review message is literal
    plain text, so it needs no escaping.
    """
    header = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("Your status", "bold"),
    ]
    if row is None:
        return _render(header + [
            ("\n\nYou have not submitted a UID yet.\n\nOpen the panel to send one.", None),
        ])
    label = STATUS_LABELS.get(status, status)
    segments = header + [
        ("\n\nStatus: ", None),
        (label, "bold"),
        ("\nUID: ", None),
        (mask_uid(row.registration_uid), "code"),
        ("\nSent: ", None),
        (row.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if row.submitted_at else "—", None),
        ("\n\n", None),
    ]
    for i, line in enumerate(_window_lines(status)):
        if i:
            segments.append(("\n", None))
        segments.append((line, None))
    if row.review_message:
        segments.append(("\n\nReview message:\n", None))
        segments.append((row.review_message, None))
    return _render(segments)


def _latest_status(telegram_id: int) -> tuple[str, Submission | None, User | None]:
    factory = get_session_factory(settings.database_url)
    db = factory()
    try:
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
    finally:
        db.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, entities = _welcome_text()
    if not settings.mini_app_url and not settings.registration_url:
        text += "\n\nThe panel and registration links are not configured yet — ask the administrator to set MINI_APP_URL and REGISTRATION_URL."
    await update.message.reply_text(text, entities=entities, reply_markup=_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, entities = _help_text(admin=_is_admin(update.effective_user.id))
    await update.message.reply_text(text, entities=entities, reply_markup=_keyboard())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status, row, _ = _latest_status(update.effective_user.id)
    text, entities = _status_card(status, row)
    await update.message.reply_text(text, entities=entities, reply_markup=_keyboard())


def _is_admin(telegram_id: int) -> bool:
    """Server-side admin check: the bot allowlist, the same IDs as the API."""
    return settings.is_admin(telegram_id)


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: the review queue, newest first, with full UIDs."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.", reply_markup=_keyboard())
        return
    db = get_session_factory(settings.database_url)()
    try:
        rows = (
            db.query(Submission)
            .filter(Submission.status.in_(ACTIVE_STATUSES))
            .order_by(desc(Submission.submitted_at))
            .limit(10)
            .all()
        )
        owners = {}
        if rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()
            }
    finally:
        db.close()
    if not rows:
        await update.message.reply_text("No submissions are waiting for review.", reply_markup=_keyboard())
        return
    lines = ["Pending submissions (newest first):"]
    for r in rows:
        when = r.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if r.submitted_at else "—"
        lines.append(
            f"#{r.id} · {r.registration_uid} · {STATUS_LABELS.get(r.status, r.status)}"
            f" · user {owners.get(r.user_id, '?')} · {when}"
        )
    lines.append("\n/approve <id> [message] · /reject <id> <message> · /needinfo <id> <message>")
    await update.message.reply_text("\n".join(lines), reply_markup=_keyboard())


async def _admin_change(update: Update, context: ContextTypes.DEFAULT_TYPE, *, to_status: str, command: str) -> None:
    """Shared /approve /reject /needinfo: ``<id> [review message]``.

    Uses the same transition as the Mini App admin (services), so the
    history, audit trail and user notification are identical either way.
    """
    admin_id = update.effective_user.id
    if not _is_admin(admin_id):
        await update.message.reply_text("This command is for admins only.", reply_markup=_keyboard())
        return
    args = list(context.args or [])
    if not args:
        await update.message.reply_text(
            f"Usage: /{command} <submission id> [review message]", reply_markup=_keyboard()
        )
        return
    try:
        submission_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            f"'{args[0]}' is not a submission id. Usage: /{command} <submission id> [review message]",
            reply_markup=_keyboard(),
        )
        return
    message = " ".join(args[1:]).strip() or None
    db = get_session_factory(settings.database_url)()
    try:
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
    finally:
        db.close()
    if error is not None:
        await update.message.reply_text(error, reply_markup=_keyboard())
        return
    notified = False
    if owner_telegram_id is not None:
        notified = await notify_user(
            settings.bot_token, owner_telegram_id, to_status, row.review_message, settings.mini_app_url
        )
    verb = {
        "verified": "verified",
        "rejected": "rejected",
        "info_required": "marked as needing more information",
    }[to_status]
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
    await update.message.reply_text(text, entities=entities, reply_markup=_keyboard())


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_change(update, context, to_status="verified", command="approve")


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_change(update, context, to_status="rejected", command="reject")


async def needinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_change(update, context, to_status="info_required", command="needinfo")


def _predict_url() -> str | None:
    """Mini App URL that opens straight on the Prediction tab."""
    if not settings.mini_app_url:
        return None
    sep = "&" if "?" in settings.mini_app_url else "?"
    return settings.mini_app_url + sep + "startapp=prediction"


async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = _predict_url()
    if url is None:
        await update.message.reply_text(
            "The verification panel is not configured yet — ask the administrator to set MINI_APP_URL."
        )
        return
    text, entities = _render([
        ("💎", ("custom", EMOJI_DIAMOND)),
        (" ", None),
        ("WinGo predictions", "bold"),
        ("\n\n", None),
        ("📈", ("custom", EMOJI_CHART_UP)),
        (" Live results, history and the next-period signal — for verified users only.", None),
        ("\n\nTap below to open the predictions.", None),
    ])
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Predictions", web_app=WebAppInfo(url=url), style="success", icon_custom_emoji_id=EMOJI_DIAMOND)]]
    )
    await update.message.reply_text(text, entities=entities, reply_markup=keyboard)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # There is no multi-step conversation in this bot, so cancel is a safe
    # no-op acknowledgement — it never deletes data.
    await update.message.reply_text(
        "Nothing pending to cancel. You can continue with /start or check /status.",
        reply_markup=_keyboard(),
    )


async def _button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "__help__":
        text, entities = _help_text(admin=_is_admin(query.from_user.id))
        await query.message.reply_text(text, entities=entities, reply_markup=_keyboard())
    elif query.data == "__status__":
        status, row, _ = _latest_status(query.from_user.id)
        text, entities = _status_card(status, row)
        await query.message.reply_text(text, entities=entities, reply_markup=_keyboard())


def register_handlers(app: Application) -> None:
    """Register every VerifyHub handler on the given Application."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("needinfo", needinfo_cmd))
    app.add_handler(CallbackQueryHandler(_button))


def build_telegram_application(bot_token: str, *, with_updater: bool) -> Application:
    """Build a PTB Application with all VerifyHub handlers registered.

    ``with_updater=True`` is for the polling worker (``python -m app.bot``);
    ``with_updater=False`` is for the webhook endpoint, which feeds updates
    to the application directly and must not start a poller.
    """
    builder = Application.builder().token(bot_token)
    if not with_updater:
        builder = builder.updater(None)
    app = builder.build()
    register_handlers(app)
    return app
