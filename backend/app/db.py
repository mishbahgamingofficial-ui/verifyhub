"""Database models and session handling.

SQLAlchemy is used so the same code runs on SQLite (local dev / tests)
and PostgreSQL (production, via DATABASE_URL=postgresql://...).
All queries go through the ORM — no string-built SQL, no raw user input
in queries. See ../../migrations/001_init.sql for the equivalent DDL.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

logger = logging.getLogger("verifyhub.db")

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    # BigInteger, NOT Integer: Telegram ids exceed the 32-bit range
    # (e.g. 8455891912 > 2,147,483,647). An Integer column makes every
    # insert/query for such a user fail on Postgres with
    # "integer out of range" — SQLite never catches this because its
    # integers are 64-bit, which is why the tests passed while production
    # submissions 503'd.
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(128), nullable=True)
    first_name = Column(String(128), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    submissions = relationship("Submission", back_populates="user", cascade="all, delete-orphan")


class Submission(Base):
    __tablename__ = "submissions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_uid = Column(String(128), nullable=False, index=True)
    reference = Column(String(128), nullable=True)
    # pending_review | under_review | verified | rejected | info_required
    status = Column(String(32), nullable=False, default="pending_review", index=True)
    review_message = Column(Text, nullable=True)  # user-visible admin message
    # Deposit proof screenshot, uploaded in step 2 of the flow and sent with
    # the UID submit as a data URL (client-side compressed JPEG). Stored as
    # TEXT so no external file storage is needed; only the admin detail
    # endpoint returns it, owner responses carry has_deposit_screenshot.
    deposit_screenshot = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    user = relationship("User", back_populates="submissions")
    history = relationship("ReviewHistory", back_populates="submission", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_submissions_user_status", "user_id", "status"),
    )


class ReviewHistory(Base):
    __tablename__ = "review_history"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    admin_telegram_id = Column(BigInteger, nullable=False)  # Telegram ids exceed 32-bit int; see User.telegram_id.
    previous_status = Column(String(32), nullable=False)
    new_status = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)       # user-visible message, if any
    internal_note = Column(Text, nullable=True)  # admin-only, never sent to users
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    submission = relationship("Submission", back_populates="history")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    actor_telegram_id = Column(BigInteger, nullable=False, index=True)  # Telegram ids exceed 32-bit int; see User.telegram_id.
    action = Column(String(64), nullable=False, index=True)
    record_type = Column(String(64), nullable=False)
    record_id = Column(Integer, nullable=True)
    meta = Column(Text, nullable=True)  # JSON, never contains tokens or full secrets
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


def make_engine(database_url: str):
    # Render's Postgres URLs use postgresql://; some providers emit the
    # legacy postgres:// scheme, which SQLAlchemy no longer accepts.
    url = database_url
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False}, future=True)
    # pool_pre_ping drops stale pooled connections (killed while idle by
    # the proxy/DB) instead of handing them to a request, where the first
    # query would fail with OperationalError. pool_recycle bounds how long
    # a pooled connection can live. This is the fix for intermittent
    # "database write fails after idle" on the free tier.
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, future=True)


# Columns that must be BIGINT on Postgres because they hold Telegram ids.
# create_all() cannot widen a column that already exists, so tables created
# before this fix still have INTEGER columns — this migration widens them
# in place (no data loss; the tables were empty when it first ran).
_TELEGRAM_ID_COLUMNS = (
    ("users", "telegram_id"),
    ("review_history", "admin_telegram_id"),
    ("audit_events", "actor_telegram_id"),
)


def ensure_bigint_telegram_ids(engine):
    """Widen INTEGER telegram-id columns to BIGINT (Postgres only).

    Idempotent: only alters columns whose current type is 'integer'.
    Never raises — a failure is logged loudly and the app keeps serving,
    so a migration problem shows up in the logs instead of crashing boot.
    """
    with engine.begin() as conn:
        for table, column in _TELEGRAM_ID_COLUMNS:
            current = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
            if current == "integer":
                logger.warning("Widening %s.%s from INTEGER to BIGINT.", table, column)
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT"))
                logger.info("Widened %s.%s to BIGINT.", table, column)


def ensure_deposit_screenshot_column(engine):
    """Add submissions.deposit_screenshot if it does not exist yet
    (Postgres only). Idempotent; never raises."""
    with engine.begin() as conn:
        exists = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'submissions' "
                "AND column_name = 'deposit_screenshot'"
            )
        ).scalar()
        if not exists:
            logger.warning("Adding submissions.deposit_screenshot column.")
            conn.execute(text("ALTER TABLE submissions ADD COLUMN deposit_screenshot TEXT"))
            logger.info("Added submissions.deposit_screenshot column.")


def make_session_factory(database_url: str):
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    if not database_url.startswith("sqlite"):
        try:
            ensure_bigint_telegram_ids(engine)
            ensure_deposit_screenshot_column(engine)
        except Exception:
            logger.exception("Failed to run startup schema migration; submissions may 503.")
    # Keep attributes loaded after commit so a failed db.refresh() after a
    # successful commit cannot turn a persisted change into a 500.
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)


_FACTORIES: dict[str, object] = {}


def get_session_factory(database_url: str):
    """Cached per DATABASE_URL so tests with a temp file reuse one engine."""
    if database_url not in _FACTORIES:
        _FACTORIES[database_url] = make_session_factory(database_url)
    return _FACTORIES[database_url]


def mask_uid(uid: str) -> str:
    """Protected display form for the owner-facing UI. Admins see the full
    UID only through the admin detail endpoint, after server-side auth."""
    if len(uid) <= 4:
        return "••••"
    return f"{uid[:2]}••••{uid[-2:]}"
