-- VerifyHub 001 — initial schema (PostgreSQL; SQLite equivalent is
-- created automatically by SQLAlchemy create_all in local dev).
-- Apply with: psql "$DATABASE_URL" -f migrations/001_init.sql
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE NOT NULL,
  username VARCHAR(128),
  first_name VARCHAR(128),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_users_telegram_id ON users (telegram_id);

CREATE TABLE IF NOT EXISTS submissions (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  registration_uid VARCHAR(128) NOT NULL,
  reference VARCHAR(128),
  status VARCHAR(32) NOT NULL DEFAULT 'pending_review'
    CHECK (status IN ('pending_review','under_review','verified','rejected','info_required')),
  review_message TEXT,
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_submissions_user_id ON submissions (user_id);
CREATE INDEX IF NOT EXISTS ix_submissions_registration_uid ON submissions (registration_uid);
CREATE INDEX IF NOT EXISTS ix_submissions_status ON submissions (status);
CREATE INDEX IF NOT EXISTS ix_submissions_user_status ON submissions (user_id, status);

CREATE TABLE IF NOT EXISTS review_history (
  id SERIAL PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  admin_telegram_id BIGINT NOT NULL,
  previous_status VARCHAR(32) NOT NULL,
  new_status VARCHAR(32) NOT NULL,
  reason TEXT,
  internal_note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_review_history_submission_id ON review_history (submission_id);

CREATE TABLE IF NOT EXISTS audit_events (
  id SERIAL PRIMARY KEY,
  actor_telegram_id BIGINT NOT NULL,
  action VARCHAR(64) NOT NULL,
  record_type VARCHAR(64) NOT NULL,
  record_id INTEGER,
  meta TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_events_actor ON audit_events (actor_telegram_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_action ON audit_events (action);
