"""FastAPI dependencies: authenticated Telegram identity + admin gate.

Admin status is NEVER taken from the client. It is derived on every
request from the validated initData user id: the server-side
ADMIN_TELEGRAM_IDS allowlist in config, or the bot-added admins table
(see services.is_admin).
"""
from __future__ import annotations

from datetime import timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import User, _utcnow
from .security import InitDataError, TelegramIdentity, validate_init_data

# last_seen_at is only written when it is older than this, so pure reads
# (status checks) do not fail when the database briefly refuses writes.
_LAST_SEEN_TOUCH_SECONDS = 300


def get_db_session(settings: Settings = Depends(get_settings)):
    from .db import get_session_factory

    factory = get_session_factory(settings.database_url)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def get_identity(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    settings: Settings = Depends(get_settings),
) -> TelegramIdentity:
    try:
        return validate_init_data(
            x_telegram_init_data or "",
            settings.bot_token,
            settings.init_data_max_age_seconds,
        )
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None


def get_current_user(
    identity: TelegramIdentity = Depends(get_identity),
    db: Session = Depends(get_db_session),
) -> User:
    """Upsert the user row (first_seen / last_seen) for the *validated* id.

    Writes are kept minimal: a new row is inserted once, and last_seen_at
    is only touched when it is stale. Two concurrent first requests can
    race the unique telegram_id — the loser re-reads the winner's row
    instead of 500ing.
    """
    user = db.query(User).filter(User.telegram_id == identity.telegram_id).first()
    if user is None:
        user = User(
            telegram_id=identity.telegram_id,
            username=identity.username,
            first_name=identity.first_name,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter(User.telegram_id == identity.telegram_id).first()
            if user is None:
                raise
        db.refresh(user)
        return user
    now = _utcnow()
    last_seen = user.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        # SQLite returns naive datetimes; treat them as UTC.
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or (now - last_seen).total_seconds() > _LAST_SEEN_TOUCH_SECONDS:
        user.username = identity.username or user.username
        user.first_name = identity.first_name or user.first_name
        user.last_seen_at = now
        db.commit()
        db.refresh(user)
    return user


def require_admin(
    identity: TelegramIdentity = Depends(get_identity),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> TelegramIdentity:
    from .services import is_admin

    if not is_admin(db, identity.telegram_id, settings):
        # Deliberately generic: do not leak whether the id exists or is admin.
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return identity
