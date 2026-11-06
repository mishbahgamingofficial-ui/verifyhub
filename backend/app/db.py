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
    # Referral attribution: the telegram id of the user whose invite link
    # this user first tapped (/start ref_<id>). Set once when the user row
    # is first created, never overwritten; NULL for organic users and
    # self-referrals (ignored). No reward is attached here — what a
    # referral earns is a product decision the owner has not made yet;
    # this column only records the link, honestly counted once the
    # referred user is admin-verified.
    referred_by_telegram_id = Column(BigInteger, nullable=True, index=True)
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
    # Automatic verification (app.autoverify): the last check outcome as
    # a JSON string {"found": bool, "deposit_total": float,
    # "days_checked": [...], "passed": bool, "reason": str} — or
    # {"reason": "provider_error", "error": ...} when the provider
    # could not be reached. Only the outcome is stored, never any
    # credential. NULL until the first check runs.
    auto_check_result = Column(Text, nullable=True)
    auto_checked_at = Column(DateTime(timezone=True), nullable=True)
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


class SupportTicket(Base):
    """One support/appeal conversation with a user.

    Before this, support was stateless: every message fired a fresh
    alert with no thread, no history and no status, and the admin's
    reply target lived in a single shared "awaiting" slot — so tapping
    Reply on user B before typing to user A silently retargeted the
    reply to B. Messages now belong to a ticket, routed by ticket id.
    Status: open (waiting for an admin) / answered (admin replied,
    waiting for the user) / closed (admin closed it; a new message from
    the user opens a fresh ticket).
    """
    __tablename__ = "support_tickets"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="open", index=True)
    first_message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    user = relationship("User")
    messages = relationship("SupportMessage", back_populates="ticket", cascade="all, delete-orphan")


class SupportMessage(Base):
    """One message inside a support ticket, from the user or an admin."""
    __tablename__ = "support_messages"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_telegram_id = Column(BigInteger, nullable=False)  # Telegram ids exceed 32-bit int; see User.telegram_id.
    is_admin = Column(Boolean, nullable=False, default=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ticket = relationship("SupportTicket", back_populates="messages")


class BroadcastDraft(Base):
    """One admin's unsent broadcast draft, persisted in Postgres.

    Broadcast drafts used to live only in PTB's in-memory user_data —
    every deploy/restart silently dropped the draft mid-broadcast. The
    bot still keeps the live copy in user_data; this row is the durable
    backup, restored when the in-memory copy is missing. One row per
    admin (an admin writes one broadcast at a time).
    """
    __tablename__ = "broadcast_drafts"
    admin_telegram_id = Column(BigInteger, primary_key=True)
    text = Column(Text, nullable=False)
    entities_json = Column(Text, nullable=True)  # JSON list of MessageEntity dicts
    pin = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ProcessedUpdate(Base):
    """Telegram webhook update ids already processed (dedup).

    Telegram retries an update after a 503/slow response; without
    tracking, the retry re-runs the handler (double support alert,
    double admin reply). Rows older than a day are pruned on insert.
    """
    __tablename__ = "processed_updates"
    update_id = Column(BigInteger, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    actor_telegram_id = Column(BigInteger, nullable=False, index=True)  # Telegram ids exceed 32-bit int; see User.telegram_id.
    action = Column(String(64), nullable=False, index=True)
    record_type = Column(String(64), nullable=False)
    record_id = Column(Integer, nullable=True)
    meta = Column(Text, nullable=True)  # JSON, never contains tokens or full secrets
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


class Admin(Base):
    """Bot-added admins: Telegram ids an admin added from the bot itself.

    The env allowlist (ADMIN_TELEGRAM_IDS) is the root: those admins can
    never be removed from the bot, and at least one always exists. Rows
    here are additional admins, added/removed with /addadmin and
    /removeadmin. Both lists grant the same access (bot commands and the
    Mini App admin tab, via services.is_admin). Created by create_all at
    startup, so no manual migration is needed.
    """
    __tablename__ = "admins"
    telegram_id = Column(BigInteger, primary_key=True)
    added_by_telegram_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class SignalRecord(Base):
    """One recorded prediction signal, settled against the real result.

    The signal accuracy tracker is an honest record, never a claim:
    a row is written when /api/prediction generates a signal for the
    next period, and settled (lazily, on read) once that period's real
    result is published. ``correct`` stays NULL while the period is
    still pending. The (timer, period) unique constraint makes
    recording idempotent — first signal wins, never overwritten, so the
    record cannot be cherry-picked after the fact.
    """
    __tablename__ = "signal_records"
    id = Column(Integer, primary_key=True)
    timer = Column(String(8), nullable=False, index=True)  # "30s" / "1m" / "3m" / "5m"
    period = Column(String(32), nullable=False)  # the WinGo issueNumber the signal was for
    signal = Column(String(8), nullable=False)  # "BIG" or "SMALL"
    reason = Column(Text, nullable=True)  # the signal reason text
    # "low" or "medium" (never "high"). Added after the tracker shipped,
    # so older rows are NULL — the accuracy endpoint counts those in the
    # "low" bucket and says so, rather than inventing a confidence.
    confidence = Column(String(8), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    actual_number = Column(Integer, nullable=True)
    actual_size = Column(String(8), nullable=True)  # "BIG" / "SMALL", once settled
    correct = Column(Boolean, nullable=True)  # True/False once settled, NULL while pending

    __table_args__ = (
        Index("ix_signal_records_timer_period", "timer", "period", unique=True),
    )


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


def ensure_signal_confidence_column(engine):
    """Add signal_records.confidence if it does not exist yet
    (Postgres only). Idempotent; never raises. Nullable, so rows
    recorded before confidence tracking simply have NULL there."""
    with engine.begin() as conn:
        exists = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'signal_records' "
                "AND column_name = 'confidence'"
            )
        ).scalar()
        if not exists:
            logger.warning("Adding signal_records.confidence column.")
            conn.execute(text("ALTER TABLE signal_records ADD COLUMN confidence VARCHAR(8)"))
            logger.info("Added signal_records.confidence column.")


def ensure_auto_check_columns(engine):
    """Add submissions.auto_check_result / auto_checked_at if they do
    not exist yet (Postgres only). Idempotent; never raises. Nullable,
    so submissions created before auto-verification simply have NULL
    there until their first check runs."""
    with engine.begin() as conn:
        for column, ddl in (
            ("auto_check_result", "ADD COLUMN auto_check_result TEXT"),
            ("auto_checked_at", "ADD COLUMN auto_checked_at TIMESTAMPTZ"),
        ):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'submissions' "
                    "AND column_name = :c"
                ),
                {"c": column},
            ).scalar()
            if not exists:
                logger.warning("Adding submissions.%s column.", column)
                conn.execute(text(f"ALTER TABLE submissions {ddl}"))
                logger.info("Added submissions.%s column.", column)


def ensure_referred_by_column(engine):
    """Add users.referred_by_telegram_id if it does not exist yet.
    Idempotent; never raises. Nullable, so users created before
    referral tracking simply have NULL there (organic).

    This is the migration the 5d2d2d1 upgrade forgot: the model gained
    the column but create_all() never adds a column to an existing
    table, so production kept serving the old users table and every
    query that loads a user row crashed with UndefinedColumn until
    this ran. New tables from the same commit (support_tickets,
    support_messages, broadcast_drafts, processed_updates) were fine —
    create_all creates missing tables, it just cannot alter one.

    Unlike the older migrations here it checks the schema through
    SQLAlchemy's inspector instead of information_schema, so the same
    code runs on Postgres and on SQLite — which is what lets the test
    suite reproduce the production failure and prove the fix."""
    from sqlalchemy import inspect

    with engine.begin() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("users")}
        if "referred_by_telegram_id" not in columns:
            logger.warning("Adding users.referred_by_telegram_id column.")
            conn.execute(text("ALTER TABLE users ADD COLUMN referred_by_telegram_id BIGINT"))
            logger.info("Added users.referred_by_telegram_id column.")
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_referred_by_telegram_id ON users (referred_by_telegram_id)"))


def make_session_factory(database_url: str):
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    if not database_url.startswith("sqlite"):
        try:
            ensure_bigint_telegram_ids(engine)
            ensure_deposit_screenshot_column(engine)
            ensure_signal_confidence_column(engine)
            ensure_auto_check_columns(engine)
            ensure_referred_by_column(engine)
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
