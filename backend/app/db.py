"""Database models and session handling.

SQLAlchemy is used so the same code runs on SQLite (local dev / tests)
and PostgreSQL (production, via DATABASE_URL=postgresql://...).
All queries go through the ORM — no string-built SQL, no raw user input
in queries. See ../../migrations/001_init.sql for the equivalent DDL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
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
    admin_telegram_id = Column(Integer, nullable=False)
    previous_status = Column(String(32), nullable=False)
    new_status = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)       # user-visible message, if any
    internal_note = Column(Text, nullable=True)  # admin-only, never sent to users
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    submission = relationship("Submission", back_populates="history")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    actor_telegram_id = Column(Integer, nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    record_type = Column(String(64), nullable=False)
    record_id = Column(Integer, nullable=True)
    meta = Column(Text, nullable=True)  # JSON, never contains tokens or full secrets
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


def make_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, future=True)


def make_session_factory(database_url: str):
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


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
