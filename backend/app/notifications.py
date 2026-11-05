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
