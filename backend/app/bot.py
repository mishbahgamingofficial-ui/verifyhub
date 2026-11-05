"""VerifyHub Telegram bot — python-telegram-bot (v20+, async).

Shares the same database as the API (app.db), so /status shows the real
latest submission, not a copy. No Mini App secret, no admin logic, and no
trust in client-sent ids lives here: the bot's own chat id *is* the
authenticated Telegram identity for bot commands.

Run (polling):
    cd backend && python -m app.bot
Polling is the supported mode in this release. A webhook endpoint can be
added later on top of the same handlers (see README § Deployment) without
changing any command logic.

Commands: /start /help /status /cancel
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import desc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import get_settings
from .db import Submission, User, get_session_factory, mask_uid

logger = logging.getLogger("verifyhub.bot")
settings = get_settings()

WELCOME = """<b>Welcome to {name}</b>

Submit your registration UID and track its verification status securely.

Complete your registration using the official link, then submit your UID through the verification panel.

Your submission will be reviewed by an administrator. Verification does not guarantee financial recovery.

Use the buttons below to continue."""

HELP_TEXT = """<b>How verification works</b>

<b>1.</b> Register using the official registration link.
<b>2.</b> Copy your account UID from that registration.
<b>3.</b> Open the verification panel and submit the UID.
<b>4.</b> An administrator reviews it. You will see the outcome here and in the panel.

<b>Commands</b>
/start — open the panel and registration link
/status — check your latest submission
/help — this guide
/cancel — cancel any pending operation

Submitting a UID does not automatically mean it has been verified.
No passwords, OTPs, or recovery phrases are ever requested."""

STATUS_LABELS = {
    "not_submitted": "Not submitted",
    "pending_review": "Pending review",
    "under_review": "Under review",
    "verified": "Verified (administrator-reviewed)",
    "rejected": "Rejected",
    "info_required": "Additional information required",
}


def _keyboard() -> InlineKeyboardMarkup:
    rows = []
    if settings.mini_app_url:
        rows.append([InlineKeyboardButton("Open Verification Panel", web_app=WebAppInfo(url=settings.mini_app_url))])
    if settings.registration_url:
        rows.append([InlineKeyboardButton("Register Account", url=settings.registration_url)])
    rows.append([
        InlineKeyboardButton("Check Status", callback_data="__status__"),
        InlineKeyboardButton("Help & Support", callback_data="__help__"),
    ])
    return InlineKeyboardMarkup(rows)


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
    name = settings.project_name
    text = WELCOME.format(name=name)
    if not settings.mini_app_url and not settings.registration_url:
        text += "\n\n<i>The panel and registration links are not configured yet — ask the administrator to set MINI_APP_URL and REGISTRATION_URL.</i>"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=_keyboard())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status, row, _ = _latest_status(update.effective_user.id)
    label = STATUS_LABELS.get(status, status)
    if row is None:
        text = "<b>Verification status</b>\n\nYou have not submitted a UID yet.\n\nOpen the verification panel to submit one."
    else:
        text = (
            f"<b>Verification status</b>\n\n"
            f"Status: <b>{label}</b>\n"
            f"UID: <code>{mask_uid(row.registration_uid)}</code>\n"
            f"Submitted: {row.submitted_at.strftime('%Y-%m-%d %H:%M UTC') if row.submitted_at else '—'}\n"
            f"Last updated: {row.updated_at.strftime('%Y-%m-%d %H:%M UTC') if row.updated_at else '—'}"
        )
        if row.review_message:
            text += f"\n\nReview message:\n{row.review_message}"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_keyboard())


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
        await query.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=_keyboard())
    elif query.data == "__status__":
        status, row, _ = _latest_status(query.from_user.id)
        label = STATUS_LABELS.get(status, status)
        if row is None:
            text = "You have not submitted a UID yet. Open the verification panel to submit one."
        else:
            text = f"Latest status: {label}\nUID: {mask_uid(row.registration_uid)}"
            if row.review_message:
                text += f"\n\n{row.review_message}"
        await query.message.reply_text(text, reply_markup=_keyboard())


def build_app() -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    from telegram.ext import CallbackQueryHandler

    app = Application.builder().token(settings.bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(_button))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = build_app()
    logger.info("Starting VerifyHub bot (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
