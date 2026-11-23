"""SQLite persistence for postings, scores, and status changes."""

import json
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path

from smartplier import analytics
from smartplier.scoring import extract_contacts


TRIAGE = ("unscored", "review", "to_apply", "low", "filtered")
STATUSES = TRIAGE + ("applied", "interview", "offer", "rejected", "withdrawn", "closed")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    url TEXT NOT NULL,
    contacts TEXT NOT NULL,
    status TEXT NOT NULL,
    score INTEGER CHECK(score BETWEEN 0 AND 100),
    score_method TEXT,
    score_details TEXT,
    location_tier INTEGER CHECK(location_tier BETWEEN 1 AND 3),
    scored_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    previous_status TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS status_history_job ON status_history(job_id, id);
CREATE TABLE IF NOT EXISTS resume_cache (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    fingerprint TEXT NOT NULL,
    selection TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_discovery (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    source TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    published_at TEXT,
    expires_at TEXT,
    reposts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_aliases (
    alias TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS discovery_aliases_job ON discovery_aliases(job_id, alias);
CREATE TABLE IF NOT EXISTS outreach (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    drafts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    channel TEXT CHECK(channel IN ('email', 'linkedin')),
    replied_at TEXT,
    followed_up_at TEXT
);
CREATE INDEX IF NOT EXISTS outreach_sent ON outreach(sent_at);
CREATE TABLE IF NOT EXISTS applications (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    applied_at TEXT NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    score INTEGER CHECK(score BETWEEN 0 AND 100),
    variant TEXT NOT NULL,
    response_at TEXT
);
"""


@contextmanager
def database(path, profile_path=None):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            db.executescript(SCHEMA)
            if version == 0:
                migrate_scores(db, profile_path)
            analytics.backfill(db)
        yield db
    finally:
        db.close()


def add_job(db, title, company, location, description, url):
    contacts = extract_contacts(description)
    with nullcontext() if db.in_transaction else db:
        row = db.execute(
            """INSERT INTO jobs (title, company, location, description, url, contacts, status)
               VALUES (?, ?, ?, ?, ?, ?, 'unscored')""",
            (title, company, location, description, url, json.dumps(contacts)),
        )
        job_id = row.lastrowid
        record_status(db, job_id, None, "unscored", "Posting added.")
    return job_id


def record_status(db, job_id, previous, status, reason):
    db.execute(
        "INSERT INTO status_history (job_id, previous_status, status, reason) VALUES (?, ?, ?, ?)",
        (job_id, previous, status, reason),
    )


def migrate_scores(db, profile_path):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            return
        threshold = 65
        jobs = db.execute("SELECT * FROM jobs WHERE score_method IS NOT NULL").fetchall()
        if any(job["score_method"] == "gemini" for job in jobs) and profile_path:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8-sig"))
            threshold = profile["preferences"]["min_score_to_apply"]
        for job in jobs:
            method, status = job["score_method"], job["status"]
            details = json.loads(job["score_details"] or "{}")
            score = None
            if method == "offline":
                method, details = None, None
                if status in TRIAGE:
                    status = "unscored"
            elif method == "gemini":
                score = details.pop("raw_fit_score", job["score"])
                details["fit_score"] = score
                if status in ("low", "to_apply"):
                    status = "to_apply" if score >= threshold else "low"
            elif method == "filter":
                details.pop("raw_fit_score", None)
                details["fit_score"] = None
            else:
                continue
            db.execute(
                """UPDATE jobs SET score = ?, score_method = ?, score_details = ?, status = ?,
                   scored_at = CASE WHEN ? IS NULL THEN NULL ELSE scored_at END,
                   location_tier = CASE WHEN ? IS NULL THEN NULL ELSE location_tier END,
                   revision = revision + 1 WHERE id = ?""",
                (score, method, json.dumps(details) if details is not None else None, status, method, method, job["id"]),
            )
            if status != job["status"]:
                record_status(db, job["id"], job["status"], status, "Updated to Gemini-only scoring.")
        db.execute("PRAGMA user_version = 1")


class JobNotFound(ValueError):
    """The requested posting does not exist."""


def get_job(db, job_id):
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise JobNotFound(f"No job with ID {job_id}.")
    return job


def history(db, job_id):
    return db.execute(
        "SELECT previous_status, status, reason, changed_at FROM status_history WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()


def change_status(db, job_id, status, reason, variant=None):
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}.")
    variant = analytics.clean_variant(variant)
    if variant and status != "applied":
        raise analytics.AnalyticsError("Set a resume variant while recording an application or using analytics --variant.")
    own_transaction = not db.in_transaction
    with db if own_transaction else nullcontext():
        # Lock before reading so a concurrent score/status update cannot stale the history.
        if own_transaction:
            db.execute("BEGIN IMMEDIATE")
        job = get_job(db, job_id)
        changed = job["status"] != status
        if changed:
            db.execute("UPDATE jobs SET status = ?, revision = revision + 1 WHERE id = ?", (status, job_id))
            record_status(db, job_id, job["status"], status, reason)
        if status == "applied":
            analytics.record_application(db, job, analytics.timestamp(), variant)
            if variant:
                changed = analytics.update_variant(db, job_id, variant) or changed
        elif changed and status in analytics.RESPONSE_STATUSES:
            analytics.record_reply(db, job_id, analytics.timestamp(), require_application=False)
    return changed


def scoring_queue(db):
    return db.execute(
        "SELECT * FROM jobs WHERE score_method IS NULL ORDER BY id",
    ).fetchall()


def update_location(db, job_id, location):
    with db:
        db.execute("BEGIN IMMEDIATE")
        job = get_job(db, job_id)
        if location == job["location"]:
            return job
        status = "unscored" if job["status"] in TRIAGE else job["status"]
        db.execute(
            """UPDATE jobs SET location = ?, status = ?, score = NULL, score_method = NULL,
               score_details = NULL, location_tier = NULL, scored_at = NULL,
               revision = revision + 1 WHERE id = ?""",
            (location, status, job_id),
        )
        record_status(db, job_id, job["status"], status, "Explicit location updated.")
        return get_job(db, job_id)


def save_score(db, job, evaluation):
    status = evaluation.status if job["status"] in TRIAGE else job["status"]
    with nullcontext() if db.in_transaction else db:
        updated = db.execute(
            """UPDATE jobs SET score = ?, score_method = ?, score_details = ?, location_tier = ?,
               status = ?, scored_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
               revision = revision + 1 WHERE id = ? AND revision = ?""",
            (
                evaluation.details["fit_score"], evaluation.method, json.dumps(evaluation.details),
                evaluation.tier, status, job["id"], job["revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(f"Job #{job['id']} changed during scoring. Run score again.")
        if status != job["status"]:
            record_status(db, job["id"], job["status"], status, evaluation.details["reason"])
    return status
