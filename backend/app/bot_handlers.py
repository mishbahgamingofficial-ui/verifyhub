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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .config import get_settings
from .db import Submission, User, get_session_factory, mask_uid

logger = logging.getLogger("verifyhub.bot")
settings = get_settings()

WELCOME = """<b>🔥 VerifyHub — one pass, one verdict.</b>

✅ Send your UID once. An admin reviews it.
🎯 Verified users unlock WinGo predictions.

Tap below to open the panel."""

HELP_TEXT = """<b>VerifyHub — how it works</b>

<b>1.</b> Register on the official site, copy your UID.
<b>2.</b> Open the panel and send the UID once.
<b>3.</b> An admin reviews it — watch the three windows.

<b>Verified</b> = admin-approved. No guarantee of recovery, and not provider-confirmed.
Verified users unlock the Prediction tab (WinGo 30s + 1 min) — a signal, never a guarantee.

No passwords, OTPs or recovery phrases are ever asked for.

<b>Commands</b>
/start — open the panel
/status — check your latest submission
/predict — open predictions (verified users)
/help — this guide
/cancel — cancel any pending operation"""

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


def _status_card(status: str, row: Submission | None) -> str:
    """Compact status card: badge word, masked UID, sent time, three windows."""
    import html as _html

    label = STATUS_LABELS.get(status, status)
    if row is None:
        return "<b>◆ Your status</b>\n\nYou have not submitted a UID yet.\n\nOpen the panel to send one."
    lines = [
        "<b>◆ Your status</b>",
        "",
        f"Status: <b>{label}</b>",
        f"UID: <code>{mask_uid(row.registration_uid)}</code>",
        f"Sent: {row.submitted_at.strftime('%Y-%m-%d %H:%M UTC') if row.submitted_at else '—'}",
        "",
    ] + _window_lines(status)
    if row.review_message:
        lines += ["", f"Review message:\n{_html.escape(row.review_message)}"]
    return "\n".join(lines)


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
    await update.message.reply_text(_status_card(status, row), parse_mode="HTML", reply_markup=_keyboard())


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
    text = (
        "<b>💎 WinGo predictions</b>\n\n"
        "📈 Live results, history and the next-period signal — for verified users only.\n\n"
        "Tap below to open the predictions."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Predictions", web_app=WebAppInfo(url=url), style="success", icon_custom_emoji_id=EMOJI_DIAMOND)]]
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


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
        await query.message.reply_text(_status_card(status, row), parse_mode="HTML", reply_markup=_keyboard())


def register_handlers(app: Application) -> None:
    """Register every VerifyHub handler on the given Application."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
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
