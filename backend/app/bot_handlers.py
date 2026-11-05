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
from datetime import datetime, timezone

from sqlalchemy import desc, func
from telegram import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import get_settings
from .db import Admin, SignalRecord, Submission, User, get_session_factory, mask_uid
from .emoji import (
    EMOJI_CHART,
    EMOJI_CHART_UP,
    EMOJI_CHECK,
    EMOJI_CROWN,
    EMOJI_DIAMOND,
    EMOJI_TARGET,
    EMOJI_THINKING,
)
from .notifications import notify_user
from .prediction import ProviderError, get_live
from .services import (
    ACTIVE_STATUSES,
    STATUSES,
    add_admin,
    change_submission_status,
    is_admin,
    list_db_admins,
    remove_admin,
)

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
        ("VerifyHub — ek baar, ek faisla.", "bold"),
        ("\n\n", None),
        ("✅", ("custom", EMOJI_CHECK)),
        (" Apna UID ek baar bhejo — admin check karega.", None),
        ("\n", None),
        ("🎯", ("custom", EMOJI_TARGET)),
        (" Verify hote hi WinGo predictions milenge.", None),
        ("\n\nNeeche wala button dabao aur panel kholo.", None),
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
        ("Buttons", "bold"),
        ("\nOpen Verification Panel — send your UID", None),
        ("\nCheck My Status — your latest submission + badges", None),
        ("\nOpen Predictions — WinGo signals (verified users)", None),
        ("\nLive Stats — platform numbers, below", None),
        ("\n\nEverything here works with buttons — no commands to type.", None),
    ]
    if admin:
        segments += [
            ("\n\n", None),
            ("Admin", "bold"),
            ("\nAdmin Panel — pending queue, one-tap decisions, and Manage Admins.", None),
            ("\nAdmin keyboard — neeche wale buttons se Panel, Pending, Admins aur Stats kholo.", None),
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


def _is_admin(telegram_id: int) -> bool:
    """Server-side admin check: env allowlist OR the bot-added admins table."""
    if settings.is_admin(telegram_id):
        return True
    db = get_session_factory(settings.database_url)()
    try:
        return is_admin(db, telegram_id, settings)
    finally:
        db.close()


def _badges_for(telegram_id: int) -> list[str]:
    """Badges earned from real data only — nothing invented or bought.

    ✅ Verified — latest submission approved by an admin.
    ⭐ Early Bird — among the first 10 users ever.
    📅 Veteran — account older than 30 days.
    🔁 Comeback — submitted more than once.
    """
    db = get_session_factory(settings.database_url)()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return []
        badges = []
        submissions = db.query(Submission).filter(Submission.user_id == user.id).all()
        if any(s.status == "verified" for s in submissions):
            badges.append("✅ Verified")
        if db.query(User).filter(User.id < user.id).count() < 10:
            badges.append("⭐ Early Bird")
        first_seen = user.first_seen_at
        if first_seen is not None:
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - first_seen).days >= 30:
                badges.append("📅 Veteran")
        if len(submissions) >= 2:
            badges.append("🔁 Comeback")
        return badges
    finally:
        db.close()


def _keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    """Main menu, organised 1-2-1: one wide button, two side by side, one wide.

    Button styles are Bot API 9.4 `style` (primary=blue, success=green);
    older Telegram clients ignore them, so the labels carry the meaning too
    (emoji + words) and at most two buttons per keyboard are styled.
    Admins get an extra Admin Panel button on its own row at the bottom.
    """
    rows = []
    if settings.mini_app_url:
        rows.append([InlineKeyboardButton("Open Verification Panel", web_app=WebAppInfo(url=settings.mini_app_url), style="primary", icon_custom_emoji_id=EMOJI_CROWN)])
    middle = [InlineKeyboardButton("Check My Status", callback_data="__status__", style="success", icon_custom_emoji_id=EMOJI_CHART)]
    predict_url = _predict_url()
    if predict_url:
        middle.append(InlineKeyboardButton("Open Predictions", web_app=WebAppInfo(url=predict_url), icon_custom_emoji_id=EMOJI_DIAMOND))
    rows.append(middle)
    rows.append([InlineKeyboardButton("Help", callback_data="__help__", icon_custom_emoji_id=EMOJI_THINKING)])
    if admin:
        rows.append([InlineKeyboardButton("🛡 Admin Panel", callback_data="__admin__", icon_custom_emoji_id=EMOJI_CROWN)])
    return InlineKeyboardMarkup(rows)


def _admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard for admins only: every admin action from
    the keyboard area (next to the message box), organised 1-2-1 like the
    main menu.

    Unlike the inline panel, this stays on screen in the chat, so an admin
    never has to scroll back to the dashboard. Tapping a button sends its
    label as a message, handled by _handle_text.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🛡 Admin Panel")],
            [KeyboardButton("📋 Pending"), KeyboardButton("👥 Admins")],
            [KeyboardButton("📊 Live Stats")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


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


def _status_card(status: str, row: Submission | None, badges: list[str] | None = None) -> tuple[str, list]:
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
        segments = header + [
            ("\n\nYou have not submitted a UID yet.\n\nOpen the panel to send one.", None),
        ]
        if badges:
            segments += [("\n\n", None), ("Badges: ", "bold"), (" · ".join(badges), None)]
        return _render(segments)
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
    if badges:
        segments += [("\n\n", None), ("Badges: ", "bold"), (" · ".join(badges), None)]
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


def _admin_dashboard() -> tuple[str, list, InlineKeyboardMarkup]:
    """Live admin overview: counts by status, today's intake, recent rows."""
    db = get_session_factory(settings.database_url)()
    try:
        rows = db.query(Submission).order_by(desc(Submission.submitted_at)).limit(5).all()
        total = db.query(Submission).count()
        counts = {s: 0 for s in STATUSES}
        for st, n in db.query(Submission.status, func.count()).group_by(Submission.status).all():
            counts[st] = n
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today = db.query(Submission).filter(Submission.submitted_at >= today_start).count()
        owners = {}
        if rows:
            owners = {
                u.id: u.telegram_id
                for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()
            }
    finally:
        db.close()
    segments = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("Admin Panel", "bold"),
        ("\n\n", None),
        (f"Total: {total}  ·  Today: {today}", None),
        ("\n", None),
        (f"Pending: {counts['pending_review']}  ·  Under review: {counts['under_review']}", None),
        ("\n", None),
        (f"Verified: {counts['verified']}  ·  Rejected: {counts['rejected']}  ·  Info needed: {counts['info_required']}", None),
        ("\n", None),
        (f"Admins: {len(settings.admin_telegram_ids) + db.query(Admin).count()}", None),
    ]
    if rows:
        segments.append(("\n\nRecent:", "bold"))
        for r in rows:
            when = r.submitted_at.strftime("%m-%d %H:%M") if r.submitted_at else "—"
            segments.append((
                f"\n#{r.id} · {r.registration_uid} · {STATUS_LABELS.get(r.status, r.status)}"
                f" · user {owners.get(r.user_id, '?')} · {when}",
                None,
            ))
    else:
        segments.append(("\n\nNo submissions yet.", None))
    text, entities = _render(segments)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Pending queue", callback_data="admin:pending"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:d"),
        ],
        [InlineKeyboardButton("👥 Manage Admins", callback_data="admin:admins")],
    ])
    return text, entities, keyboard


def _admins_view() -> tuple[str, InlineKeyboardMarkup]:
    """Every admin (server config first, then bot-added) with remove buttons."""
    db = get_session_factory(settings.database_url)()
    try:
        rows = list_db_admins(db)
    finally:
        db.close()
    lines = ["Admins:"]
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


def _pending_view() -> tuple[str, InlineKeyboardMarkup]:
    """The review queue as text plus one-tap action buttons per row."""
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
    nav = [[
        InlineKeyboardButton("📊 Admin Panel", callback_data="admin:dashboard"),
        InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:p"),
    ]]
    if not rows:
        return "No submissions are waiting for review.", InlineKeyboardMarkup(nav)
    lines = ["Pending submissions (newest first):"]
    keyboard_rows = []
    for r in rows:
        when = r.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if r.submitted_at else "—"
        lines.append(
            f"#{r.id} · {r.registration_uid} · {STATUS_LABELS.get(r.status, r.status)}"
            f" · user {owners.get(r.user_id, '?')} · {when}"
        )
        keyboard_rows.append([
            InlineKeyboardButton(f"✅ #{r.id}", callback_data=f"admin:approve:{r.id}:p"),
            InlineKeyboardButton(f"❌ #{r.id}", callback_data=f"admin:reject:{r.id}:p"),
            InlineKeyboardButton(f"ℹ️ #{r.id}", callback_data=f"admin:info:{r.id}:p"),
        ])
    lines.append("\nTap ✅ to approve, ❌ to reject, ℹ️ to ask for more info.")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows + nav)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin = _is_admin(update.effective_user.id)
    text, entities = _welcome_text()
    if not settings.mini_app_url and not settings.registration_url:
        text += "\n\nThe panel and registration links are not configured yet — ask the administrator to set MINI_APP_URL and REGISTRATION_URL."
    await update.message.reply_text(text, entities=entities, reply_markup=_keyboard(admin=admin))
    if admin:
        # An admin opening the bot sees the whole picture right away.
        dtext, dentities, dkeyboard = _admin_dashboard()
        await update.message.reply_text(dtext, entities=dentities, reply_markup=dkeyboard)
        # Plus the persistent admin keyboard in the keyboard area.
        await update.message.reply_text(
            "Admin keyboard 👇 — neeche wale buttons se Panel, Pending, Admins aur Stats kholo.",
            reply_markup=_admin_reply_keyboard(),
        )


def _stats_text(telegram_id: int) -> tuple[str, list]:
    """Public live stats: real platform numbers + the caller's badges."""
    db = get_session_factory(settings.database_url)()
    try:
        total_users = db.query(User).count()
        total_subs = db.query(Submission).count()
        verified = db.query(Submission).filter(Submission.status == "verified").count()
        pending = db.query(Submission).filter(Submission.status.in_(ACTIVE_STATUSES)).count()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        verified_today = db.query(Submission).filter(
            Submission.status == "verified", Submission.updated_at >= today_start
        ).count()
        acc_rows = db.query(SignalRecord).filter(SignalRecord.timer == "1m").all()
        settled = [r for r in acc_rows if r.correct is not None]
        correct = sum(1 for r in settled if r.correct)
    finally:
        db.close()
    badges = _badges_for(telegram_id)
    segments = [
        ("📊", ("custom", EMOJI_CHART)),
        (" ", None),
        ("VerifyHub — live stats", "bold"),
        ("\n\n", None),
        (f"Users: {total_users}  ·  Submissions: {total_subs}", None),
        ("\n", None),
        (f"Verified: {verified}  ·  Verified today: {verified_today}  ·  Waiting: {pending}", None),
    ]
    if settled:
        acc = round(correct / len(settled) * 100, 1)
        segments.append((f"\nSignals (1m): {len(settled)} settled, {correct} right — {acc}%", None))
    else:
        segments.append(("\nSignals (1m): no settled signals yet.", None))
    try:
        live = get_live()
        segments.append((f"\n\nLive WinGo 1m: period …{live['period'][-4:]} ends in {live['ends_in_seconds']}s", None))
    except Exception:
        logger.debug("Live period unavailable for stats.")
    if badges:
        segments.append(("\n\nYour badges: ", "bold"))
        segments.append((" · ".join(badges), None))
    else:
        segments.append(("\n\nNo badges yet — get verified to earn your first one.", None))
    return _render(segments)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text replies: admin reply-keyboard buttons + the add-admin ID prompt.

    Tapping an admin keyboard button sends its label as a message — that is
    dispatched here first (it also cancels a half-finished add-admin prompt).
    When an admin taps ➕ Add Admin, the bot asks for a Telegram ID and waits
    for the reply here (a context.user_data flag). Anything else is ignored —
    the bot is button-driven, so stray text gets a gentle nudge.
    """
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if _is_admin(user_id):
        if text == "🛡 Admin Panel":
            context.user_data.pop("awaiting", None)
            dtext, dentities, dkeyboard = _admin_dashboard()
            await update.message.reply_text(dtext, entities=dentities, reply_markup=dkeyboard)
            return
        if text == "📋 Pending":
            context.user_data.pop("awaiting", None)
            ptext, pkeyboard = _pending_view()
            await update.message.reply_text(ptext, reply_markup=pkeyboard)
            return
        if text == "👥 Admins":
            context.user_data.pop("awaiting", None)
            atext, akeyboard = _admins_view()
            await update.message.reply_text(atext, reply_markup=akeyboard)
            return
        if text == "📊 Live Stats":
            context.user_data.pop("awaiting", None)
            stext, sentities = _stats_text(user_id)
            await update.message.reply_text(stext, entities=sentities, reply_markup=_keyboard(admin=True))
            return
    if context.user_data.get("awaiting") == "add_admin" and _is_admin(user_id):
        # The prompt stays open until the admin succeeds or taps Cancel,
        # so a wrong number is just a retry, not a dead end.
        if not text.isdigit():
            await update.message.reply_text(
                "That doesn't look like a Telegram ID — send just the number.",
                reply_markup=_keyboard(admin=True),
            )
            return
        new_id = int(text)
        if settings.is_admin(new_id):
            await update.message.reply_text(
                "That user is already an admin (set in the server config).",
                reply_markup=_keyboard(admin=True),
            )
            return
        db = get_session_factory(settings.database_url)()
        try:
            ok, error = add_admin(db, telegram_id=new_id, added_by=user_id)
        finally:
            db.close()
        if not ok:
            await update.message.reply_text(error, reply_markup=_keyboard(admin=True))
            return
        context.user_data.pop("awaiting", None)
        reply_text, entities = _render([
            ("✅", ("custom", EMOJI_CHECK)),
            (f" {new_id} is now an admin. They get the Admin Panel button and full admin access.", None),
        ])
        await update.message.reply_text(reply_text, entities=entities, reply_markup=_keyboard(admin=True))
        return
    await update.message.reply_text(
        "Use the buttons below — tap /start if they are not showing.",
        reply_markup=_keyboard(admin=_is_admin(user_id)),
    )


async def _do_admin_action(admin_id: int, submission_id: int, to_status: str, message: str | None):
    """Run the shared transition + user notification.

    Returns ``(text, entities, error)``: on success the confirmation to show
    the admin; on failure ``(None, None, human-readable reason)``.
    """
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
    return text, entities, None


def _predict_url() -> str | None:
    """Mini App URL that opens straight on the Prediction tab."""
    if not settings.mini_app_url:
        return None
    sep = "&" if "?" in settings.mini_app_url else "?"
    return settings.mini_app_url + sep + "startapp=prediction"


async def _button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    admin = _is_admin(admin_id)
    data = query.data or ""
    if data == "__help__":
        text, entities = _help_text(admin=admin)
        help_rows = [[InlineKeyboardButton("📊 Live Stats", callback_data="__stats__", icon_custom_emoji_id=EMOJI_CHART)]]
        help_rows += _keyboard(admin=admin).inline_keyboard
        await query.message.reply_text(text, entities=entities, reply_markup=InlineKeyboardMarkup(help_rows))
    elif data == "__stats__":
        text, entities = _stats_text(admin_id)
        await query.edit_message_text(text, entities=entities, reply_markup=_keyboard(admin=admin))
    elif data == "__status__":
        status, row, _ = _latest_status(admin_id)
        text, entities = _status_card(status, row, badges=_badges_for(admin_id))
        await query.message.reply_text(text, entities=entities, reply_markup=_keyboard(admin=admin))
    elif data == "__admin__" or data.startswith("admin:"):
        if not admin:
            await query.message.reply_text("This command is for admins only.", reply_markup=_keyboard())
            return
        if data == "__admin__" or data == "admin:dashboard":
            text, entities, keyboard = _admin_dashboard()
            if data == "__admin__":
                await query.message.reply_text(text, entities=entities, reply_markup=keyboard)
            else:
                await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
            return
        if data == "admin:pending":
            text, keyboard = _pending_view()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if data in ("admin:refresh:d", "admin:refresh:p"):
            if data.endswith(":d"):
                text, entities, keyboard = _admin_dashboard()
                await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
            else:
                text, keyboard = _pending_view()
                await query.edit_message_text(text, reply_markup=keyboard)
            return
        if data == "admin:admins":
            text, keyboard = _admins_view()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        if data == "admin:add":
            context.user_data["awaiting"] = "add_admin"
            await query.edit_message_text(
                "Send the Telegram ID of the new admin (just the number).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖️ Cancel", callback_data="admin:cancel-add"),
                ]]),
            )
            return
        if data == "admin:cancel-add":
            context.user_data.pop("awaiting", None)
            text, keyboard = _admins_view()
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "remove":
            try:
                target = int(parts[2])
            except ValueError:
                return
            if target == admin_id:
                await query.message.reply_text("You cannot remove yourself.", reply_markup=_keyboard(admin=True))
                return
            if settings.is_admin(target):
                await query.message.reply_text(
                    "That admin is set in the server config — remove them from ADMIN_TELEGRAM_IDS on Render.",
                    reply_markup=_keyboard(admin=True),
                )
                return
            db = get_session_factory(settings.database_url)()
            try:
                ok, error = remove_admin(db, telegram_id=target)
            finally:
                db.close()
            if not ok:
                await query.message.reply_text(error, reply_markup=_keyboard(admin=True))
                return
            text, entities = _render([
                ("✅", ("custom", EMOJI_CHECK)),
                (f" {target} is no longer an admin.", None),
            ])
            atext, akeyboard = _admins_view()
            await query.edit_message_text(
                text + "\n\n" + atext, entities=entities, reply_markup=akeyboard
            )
            return
        if len(parts) == 4 and parts[1] in ("approve", "reject", "info"):
            try:
                sid = int(parts[2])
            except ValueError:
                return
            to_status = {"approve": "verified", "reject": "rejected", "info": "info_required"}[parts[1]]
            # One-tap decisions carry a default review message; a custom
            # message can be set from the Mini App admin tab instead.
            message = {
                "verified": None,
                "rejected": "Your submission was not approved by the administrator.",
                "info_required": "Additional information is required. Please check the verification panel.",
            }[to_status]
            text, entities, error = await _do_admin_action(admin_id, sid, to_status, message)
            if error is not None:
                await query.message.reply_text(error, reply_markup=_keyboard(admin=True))
                return
            if parts[3] == "n":
                # The new-submission alert: replace it with the outcome.
                await query.edit_message_text(text, entities=entities)
            else:
                # The pending queue: confirmation on top, refreshed queue below.
                ptext, pkeyboard = _pending_view()
                await query.edit_message_text(
                    text + "\n\n" + ptext, entities=entities, reply_markup=pkeyboard
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
        admin_ids = list(settings.admin_telegram_ids)
        db = get_session_factory(settings.database_url)()
        try:
            admin_ids += [r.telegram_id for r in list_db_admins(db)]
        finally:
            db.close()
        for admin_id in dict.fromkeys(admin_ids):
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
