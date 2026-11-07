"""The current SQLite schema, connections, and explicit backups."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, token TEXT NOT NULL,
 last_attempt_at TEXT, last_success_at TEXT, last_error TEXT, job_count INTEGER,
 pending_drop TEXT, warning TEXT,
 UNIQUE(source,company), UNIQUE(source,token)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, requisition_id TEXT NOT NULL,
 title TEXT NOT NULL, location_raw TEXT NOT NULL, location_normalized TEXT NOT NULL,
 url TEXT NOT NULL, description_text TEXT NOT NULL,
 posted_at TEXT, updated_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','closed')), role_family TEXT NOT NULL,
 matches_target INTEGER NOT NULL CHECK(matches_target IN (0,1)),
 restriction_note TEXT, country TEXT NOT NULL CHECK(country IN ('US','non-US','unknown')),
 degree_note TEXT, season_evidence TEXT, season_conflict INTEGER NOT NULL DEFAULT 0,
 part_time INTEGER NOT NULL DEFAULT 0, resolved_url TEXT, posting_description TEXT,
 listing_description TEXT, pay_hourly_usd REAL, duplicate_of INTEGER REFERENCES jobs(id),
 UNIQUE(source,company,requisition_id),
 FOREIGN KEY(source,company) REFERENCES sources(source,company)
);
CREATE INDEX IF NOT EXISTS jobs_search ON jobs(status,role_family,country,matches_target);
CREATE TABLE IF NOT EXISTS applications (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 status TEXT NOT NULL
 CHECK(status IN ('interested','applying','applied','skipped','rejected','interview','offer')),
 resume_variant TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, applied_at TEXT
);
CREATE TABLE IF NOT EXISTS heartbeat (runner_id TEXT PRIMARY KEY,last_ok_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS url_resolutions (
 url TEXT PRIMARY KEY, resolved_url TEXT NOT NULL, attempted_at TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS inbox_aliases (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('handshake','linkedin')),
 external_id TEXT NOT NULL, job_id INTEGER NOT NULL REFERENCES jobs(id), UNIQUE(source,external_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if not readonly:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
        return db
    except BaseException:
        db.close()
        raise


def backup(db: sqlite3.Connection, out: Path) -> Path:
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('xb'):
        out.chmod(0o600)
    target = sqlite3.connect(out)
    try:
        db.backup(target)
    finally:
        target.close()
    return out


def audit(db: sqlite3.Connection, action: str, entity: str, detail: str = '{}') -> None:
    db.execute('INSERT INTO audit_log(action,entity,detail) VALUES(?,?,?)', (action, entity, detail))
