"""User notifications after a status change is committed.

Contract (see spec §5): only send AFTER the DB transaction commits.
Failures here never roll back the status change — they are logged and
the admin response still succeeds. No tokens are logged.
"""
from __future__ import annotations

import html
import logging

import httpx

logger = logging.getLogger("verifyhub.notifications")

STATUS_COPY = {
    "pending_review": "Your submission is now pending review.",
    "under_review": "Your submission is now under review by an administrator.",
    "verified": "Good news — your submission has been marked as verified by an administrator.",
    "rejected": "Your submission was not approved. Please check the review message in the panel.",
    "info_required": "An administrator needs additional information. Please check the review message in the panel.",
}


async def notify_user(bot_token: str, telegram_id: int, status: str, message: str | None, mini_app_url: str) -> bool:
    if not bot_token:
        return False
    text = STATUS_COPY.get(status, f"Your submission status changed to {status}.")
    if message:
        # The payload uses parse_mode=HTML, so escape the admin's message:
        # a raw "<" or "&" would make Telegram reject the whole notification.
        text += f"\n\nReview message:\n{html.escape(message)}"
    text += (
        "\n\nVerification does not guarantee financial recovery. "
        "Open the verification panel to see the full status."
    )
    payload: dict = {"chat_id": telegram_id, "text": text, "parse_mode": "HTML"}
    if mini_app_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "Open Verification Panel", "web_app": {"url": mini_app_url}}]]
        }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload)
            if resp.status_code != 200:
                logger.warning("Telegram notify failed: status=%s", resp.status_code)
                return False
            return True
    except Exception:
        logger.exception("Telegram notify error (telegram_id redacted from payload)")
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
        "Tap to decide, or use /pending for the full queue."
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"admin:approve:{submission_id}:n"},
            {"text": "❌ Reject", "callback_data": f"admin:reject:{submission_id}:n"},
            {"text": "ℹ️ Need info", "callback_data": f"admin:info:{submission_id}:n"},
        ]]
    }
    if mini_app_url:
        keyboard["inline_keyboard"].append(
            [{"text": "Open Verification Panel", "web_app": {"url": mini_app_url}}]
        )
    sent = 0
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            for admin_id in admin_ids:
                payload: dict = {"chat_id": admin_id, "text": text, "reply_markup": keyboard}
                try:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload
                    )
                    if resp.status_code == 200:
                        sent += 1
                    else:
                        logger.warning("Telegram admin notify failed: status=%s", resp.status_code)
                except Exception:
                    logger.exception("Telegram admin notify error (admin id redacted)")
    except Exception:
        logger.exception("Telegram admin notify client error")
    return sent
