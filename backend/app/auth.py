"""FastAPI dependencies: authenticated Telegram identity + admin gate.

Admin status is NEVER taken from the client. It is derived on every
request from the validated initData user id against the server-side
ADMIN_TELEGRAM_IDS allowlist in config.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import User, _utcnow
from .security import InitDataError, TelegramIdentity, validate_init_data


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
    """Upsert the user row (first_seen / last_seen) for the *validated* id."""
    user = db.query(User).filter(User.telegram_id == identity.telegram_id).first()
    if user is None:
        user = User(
            telegram_id=identity.telegram_id,
            username=identity.username,
            first_name=identity.first_name,
        )
        db.add(user)
    else:
        user.username = identity.username or user.username
        user.first_name = identity.first_name or user.first_name
        user.last_seen_at = _utcnow()
    db.commit()
    db.refresh(user)
    return user


def require_admin(
    identity: TelegramIdentity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> TelegramIdentity:
    if not settings.is_admin(identity.telegram_id):
        # Deliberately generic: do not leak whether the id exists or is admin.
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return identity
