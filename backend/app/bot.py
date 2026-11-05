"""VerifyHub Telegram bot — polling entry point (python-telegram-bot v20+, async).

Shares the same database as the API (app.db), so /status shows the real
latest submission, not a copy. No Mini App secret, no admin logic, and no
trust in client-sent ids lives here: the bot's own chat id *is* the
authenticated Telegram identity for bot commands.

All command / button behaviour lives in :mod:`app.bot_handlers` and is
shared with the webhook endpoint (``POST /telegram/webhook`` in the API),
so polling and webhook run the exact same handlers.

Run (polling):
    cd backend && python -m app.bot

Polling is the mode the paid Render Background Worker uses. On the free
tier the bot instead runs via webhook inside the API service — same
handlers, no worker needed.

Commands: /start opens the panel; everything else is an inline button,
plus a persistent reply keyboard for admins (see
bot_handlers._admin_reply_keyboard). There is no command menu — the bot
clears it at startup (see bot_handlers.clear_bot_commands).
"""
from __future__ import annotations

import logging

from telegram import Update

from .bot_handlers import build_telegram_application
from .config import get_settings

logger = logging.getLogger("verifyhub.bot")
settings = get_settings()


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
