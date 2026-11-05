"""Telegram Premium custom emoji IDs used by the bot.

Resolved from the user's own sets via getStickerSet (see MEMORY.md);
the bot owner has Telegram Premium, which is what lets the bot use them.
Older clients ignore a custom emoji icon and show the plain label instead.

Kept in its own module so both the PTB handlers (buttons + message
entities) and the raw Bot API calls in notifications.py can use them
without a circular import.
"""
from __future__ import annotations

EMOJI_CROWN = "6052853159204757225"      # 👑 from emoji_H_M_by_TgEmodziBot
EMOJI_CHART = "5190806721286657692"      # 📊 from FinanceEmoji
EMOJI_DIAMOND = "6053338597883386982"   # 💎 from emoji_H_M_by_TgEmodziBot
EMOJI_THINKING = "6334471179801200139"  # 🧐 from peachgomaemoji
EMOJI_CHECK = "6055128392360010233"      # ✅ from emoji_H_M_by_TgEmodziBot
EMOJI_TARGET = "5310278924616356636"     # 🎯 from FinanceEmoji
EMOJI_CHART_UP = "5197503331215361533"   # 📈 from FinanceEmoji
