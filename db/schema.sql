-- Schema v1. Idempotent: safe to run on every deploy.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner','admin')),
  pw_hash TEXT, pw_salt TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscribers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  email TEXT NOT NULL COLLATE NOCASE,
  current_country TEXT, current_city TEXT,
  cv_key TEXT, cv_text TEXT, cv_hash TEXT,
  send_mode TEXT NOT NULL DEFAULT 'central' CHECK (send_mode IN ('app_password','central')),
  app_password_enc TEXT,
  sub_start TEXT NOT NULL, sub_end TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0,
  pw_hash TEXT, pw_salt TEXT,
  lang TEXT NOT NULL DEFAULT 'en',
  created_by INTEGER REFERENCES staff(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriber_cities (
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  country TEXT NOT NULL, city TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (subscriber_id, country, city)
);

CREATE TABLE IF NOT EXISTS subscriber_fields (
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  auto_picked INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (subscriber_id, field)
);

-- Shared job store (collected once per field x city)
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fp TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, company TEXT,
  country TEXT, city TEXT, field TEXT,
  url TEXT, apply_email TEXT, source TEXT,
  description TEXT, posted_at TEXT,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS jobs_field_city ON jobs(field, country, city);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  score INTEGER, field TEXT,
  method TEXT NOT NULL CHECK (method IN ('email','manual')),
  recipient TEXT, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT,
  UNIQUE (subscriber_id, job_id)
);
CREATE INDEX IF NOT EXISTS apps_sub_day ON applications(subscriber_id, created_at);

CREATE TABLE IF NOT EXISTS tailored_cvs (
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  content_json TEXT, pdf_key TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (subscriber_id, field)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  place TEXT NOT NULL CHECK (place IN ('cloud','device')),
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT, status TEXT NOT NULL DEFAULT 'running', stats_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('staff','sub')),
  user_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL
);

-- One-time links: admin invites + password resets
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('staff','sub')),
  user_id INTEGER NOT NULL,
  purpose TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_fails (
  key TEXT NOT NULL, at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id INTEGER, action TEXT NOT NULL, detail TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);


-- v2: CV file metadata (data kept here as base64 only while R2 is not enabled)
CREATE TABLE IF NOT EXISTS cv_files (
  subscriber_id INTEGER PRIMARY KEY REFERENCES subscribers(id) ON DELETE CASCADE,
  name TEXT NOT NULL, mime TEXT, size INTEGER NOT NULL,
  store TEXT NOT NULL CHECK (store IN ('r2','d1')),
  data TEXT,
  uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v3: tailored CV PDFs kept in D1 (base64) — no R2 needed (fully free)
CREATE TABLE IF NOT EXISTS tailored_pdfs (
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  data TEXT NOT NULL,
  size INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (subscriber_id, field)
);

-- v4 (phase 4): subscriber "forgot password" requests, shown to staff until a new link is generated
CREATE TABLE IF NOT EXISTS reset_requests (
  subscriber_id INTEGER PRIMARY KEY REFERENCES subscribers(id) ON DELETE CASCADE,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
