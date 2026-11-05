# VerifyHub — Premium Telegram Loss-Recovery Verification Platform

A real, production-oriented system — not a mockup:

- **Telegram bot** (`backend/app/bot.py`) — `/start`, `/help`, `/status`, `/cancel`, with a Web App button and a configurable registration link.
- **Telegram Mini App** (`frontend/`) — v7 “One Pass” UI: one-time UID send, then three verification windows (Received / Under review / Decision), plus the server-gated admin dashboard in the same app.
- **Backend API** (`backend/app/main.py`) — FastAPI + SQLAlchemy, SQLite locally, PostgreSQL in production.
- **Security** — cryptographic Telegram `initData` validation on every request, server-side admin allowlist, validated status transitions, audit log, rate limiting, masked UIDs in the user UI.

> Honest status language: “Verified” in this system means **administrator-reviewed and approved**. It does **not** mean the registration was independently confirmed by a registration provider, unless a provider integration is added later (see `backend/app/services.py: provider_check()`). The UI and bot copy say this explicitly.

## What was NOT done yet (needs your real values)

This repo contains **no real credentials, no live bot, and no deployed URL**. To go live you must:

1. Create a bot with [@BotFather](https://t.me/BotFather), copy its token into `.env` as `BOT_TOKEN`.
2. Set `REGISTRATION_URL` to the legitimate registration site.
3. Deploy the API + frontend over HTTPS, set `MINI_APP_URL` to that URL, and set the bot's Menu Button / Web App URL to it in BotFather.
4. Put your Telegram user ID in `ADMIN_TELEGRAM_IDS`.
5. Start the bot (polling) and the API (see below).

Nothing here claims a deployment or provider integration succeeded — those steps are yours to run with your own accounts.

## Quick start (local)

```bash
cd verification-platform
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
# Edit .env: BOT_TOKEN, REGISTRATION_URL, MINI_APP_URL=http://localhost:8000/, ADMIN_TELEGRAM_IDS=<your id>
# For local Mini App testing without Telegram, open http://localhost:8000/
# — you will see the browser-preview banner; real submission requires opening
#   the app from Telegram so initData is present.

# Terminal 1 — API + Mini App (serves frontend/ automatically)
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — Telegram bot (polling; needs a real BOT_TOKEN)
cd backend && python -m app.bot
```

Then in Telegram, send `/start` to your bot. You should see the welcome message, **Open Verification Panel**, **Register Account**, **Check Status**, and **Help & Support**.

## Configuration

See `.env.example`. Key variables:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Bot token (also the initData signing key — never expose to frontend) |
| `BOT_USERNAME` | Bot username, without `@` |
| `MINI_APP_URL` | HTTPS URL of the Mini App |
| `REGISTRATION_URL` | Official registration site |
| `DATABASE_URL` | `sqlite:///./verifyhub.db` locally, `postgresql://…` in production |
| `ADMIN_TELEGRAM_IDS` | Comma-separated admin Telegram IDs (server-side only) |
| `UID_PATTERN` / `UID_PATTERN_DESCRIPTION` | Server-side UID validation rule |
| `PROJECT_NAME` | Display name |

## Architecture

```
verification-platform/
  frontend/          index.html, styles.css, app.js  (Telegram Web App, vanilla JS)
                     Design system v7 (One Pass — a full rebuild, strict-rules):
                     single-column phone-first flow, max-width 560px, sharp corners
                     (radius 0), bone background #edeae2, ink text, oxide accent,
                     Fraunces display + Inter UI + JetBrains Mono data. Phase 1:
                     send the UID once in a perforated ticket; Phase 2: three
                     verification windows (Received / Under review / Decision) and
                     the form locks — resubmission only when an administrator asks
                     for more information. No gradients, no glass, no glow, no blobs,
                     no repetitive rounded cards, no unnecessary animation. Telegram
                     header/background colours match the bone (#edeae2).
  backend/app/
    config.py        env-only settings
    db.py            SQLAlchemy models: users, submissions, review_history, audit_events
    security.py      initData HMAC-SHA256 validation (Telegram spec)
    auth.py          get_identity / get_current_user / require_admin dependencies
    services.py      status machine + provider_check() extension point
    notifications.py notify AFTER commit only, best-effort, never rolls back a decision
    rate_limit.py    in-memory sliding window (swap for Redis in multi-worker prod)
    main.py          FastAPI routes (user + admin)
    bot.py           python-telegram-bot commands, shares the same DB as the API
  migrations/001_init.sql   PostgreSQL DDL (SQLite schema is auto-created in dev)
  tests/test_api.py  auth, submission, duplicate, admin-only, transition tests
```

### API summary

- `GET /api/config` — public, non-secret config only.
- `GET /api/me`, `GET /api/me/submissions`, `GET /api/me/latest` — authenticated, own data only.
- `POST /api/submissions` — validated UID, confirmation checkbox, duplicate-active protection, rate-limited.
- `GET /api/admin/stats`, `GET /api/admin/submissions`, `GET /api/admin/submissions/{id}` — admin only.
- `POST /api/admin/submissions/{id}/status` — admin only, validated transition, requires a message for Rejected / Info required, writes history + audit, then notifies the user.
- `GET /api/admin/audit` — admin only.

### Status machine

```
pending_review → under_review / verified / rejected / info_required
under_review   → pending_review / verified / rejected / info_required
info_required  → pending_review / under_review / verified / rejected
verified, rejected are terminal (no outgoing transitions)
```

A user with an active (`pending_review` / `under_review`) submission cannot submit another until it resolves. UIDs are **not** assumed globally unique.

### Security checklist (implemented)

- initData validated with HMAC-SHA256 + `auth_date` freshness on every authenticated request.
- Admin = server-side allowlist checked on every admin endpoint; frontend hiding is cosmetic only.
- No bot token, secret, or admin list in frontend code, HTML, or JS.
- ORM / parameterized queries only; client text is HTML-escaped in the UI.
- Rate limiting on submissions and API calls; duplicate-submission protection.
- Audit log records actor, action, record, previous/new status, and timestamp for every privileged change.
- Notifications are sent only after the DB commit succeeds.

## Deployment (HTTPS)

**Split hosting (Hostinger website + API, Render bot):** follow
`deploy/DEPLOY-GUIDE.md` — it has the step-by-step (Render PostgreSQL +
bot worker via `render.yaml`, Hostinger VPS via `deploy/hostinger/`,
BotFather menu button, test checklist, and a Plan B for Hostinger shared
hosting, which cannot run a persistent FastAPI server). The summary below
is the generic version.

1. Put the API behind HTTPS (reverse proxy, e.g. Caddy/Nginx + TLS, or a PaaS with TLS).
2. Serve `frontend/` from the same origin or a static host at `MINI_APP_URL`.
3. Set `DATABASE_URL` to PostgreSQL. Tables are created automatically on startup (`Base.metadata.create_all`); `migrations/001_init.sql` is the equivalent DDL for reference.
4. In BotFather: `/setmenubutton` → Web App → `MINI_APP_URL`; also set the bot's description to match the `/start` copy.
5. Run with a process manager:
   ```bash
   cd backend
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
   python -m app.bot   # separate process, polling
   ```
   Note: the in-memory rate limiter is per-process; with multiple API workers, move it to Redis (same `check()` interface in `rate_limit.py`).
6. Webhook mode for the bot is **not** wired in this release — polling is the supported mode. It can be added on top of the same handlers without changing command logic.

## Tests

```bash
cd verification-platform
pip install -r backend/requirements.txt
python -m pytest tests/ -q
```

Tests cover: public config contains no secrets, missing/forged initData rejected, UID validation + confirmation, duplicate protection, user isolation (no IDOR), admin-only enforcement, required messages, valid/invalid transitions, history + audit, and stats derived from real rows.

## Extending

- **Provider integration:** implement `provider_check()` in `services.py` and record its evidence in `ReviewHistory`/`AuditEvent`. Do not let it silently flip a status without admin review unless you deliberately design that policy and document it.
- **Additional workflow stages:** add new statuses to `STATUSES` in `services.py`, the transition map, the migration CHECK constraint, and the frontend labels — in that order.
- **More admin roles:** replace the boolean allowlist check in `require_admin` with a roles table; keep the check server-side.

## Existing project note

The workspace already contained an unrelated bot (`workspace/user/files/bot.py`, a separate Telethon project). It was **left untouched** — this platform is a new, independent codebase in `verification-platform/`.
