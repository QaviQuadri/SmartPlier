"""Application snapshots, response rates and aggregate scoring feedback."""

import re
from datetime import datetime, timedelta, timezone

from smartplier.scoring import has_phrase


RESPONSE_STATUSES = ("interview", "offer", "rejected")
MIN_SAMPLE = 5
MATURITY_DAYS = 14
UNRECORDED = "Unrecorded"
DIMENSIONS = (
    ("role", "Role type"),
    ("source", "Source"),
    ("score_band", "Score band"),
    ("variant", "Resume variant"),
)
ROLE_TYPES = (
    ("AI / ML", ("machine learning", "ml", "mlops", "artificial intelligence", "ai", "genai", "gen ai",
                 "generative ai", "llm", "nlp", "computer vision", "applied scientist")),
    ("Data", ("data", "analytics engineer", "business intelligence", "bi analyst")),
    ("Python / Backend", ("python", "backend", "back-end", "back end")),
    ("Software Engineering", ("software", "developer", "sde", "full stack", "full-stack", "frontend",
                              "front-end", "platform engineer")),
    ("Graduate / Trainee", ("graduate", "trainee", "intern")),
)
SCORE_BANDS = ("0–49", "50–64", "65–79", "80–100", "Unscored")
ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "lever-eu": "Lever", "ashby": "Ashby", "workable": "Workable"}
MAIL_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri", "bayt": "Bayt",
               "gulftalent": "GulfTalent", "naukrigulf": "Naukrigulf"}


class AnalyticsError(ValueError):
    """An application analytics error safe to display."""


def role_type(title):
    return next((label for label, terms in ROLE_TYPES if any(has_phrase(title, term) for term in terms)), "Other")


def score_band(score):
    if score is None:
        return "Unscored"
    if score < 50:
        return SCORE_BANDS[0]
    if score < 65:
        return SCORE_BANDS[1]
    return SCORE_BANDS[2 if score < 80 else 3]


def date(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def timestamp(now=None):
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def source_for(db, job_id, applied_at=None):
    found = db.execute("SELECT source, first_seen FROM job_discovery WHERE job_id = ?", (job_id,)).fetchone()
    if not found or applied_at and date(found["first_seen"]) > date(applied_at):
        return "Manual"
    label = found["source"]
    if label == "Gmail job alerts":
        aliases = db.execute(
            """SELECT alias FROM discovery_aliases WHERE job_id = ? AND alias LIKE 'id:gmail:%' ORDER BY alias""",
            (job_id,),
        )
        labels = {MAIL_LABELS.get(row["alias"].split(":", 3)[2]) for row in aliases}
        labels.discard(None)
        if len(labels) == 1:
            return f"Gmail · {labels.pop()}"
        return label
    provider = re.search(r"\((greenhouse|lever|lever-eu|ashby|workable)\)$", label, re.I)
    return ATS_LABELS[provider[1].lower()] if provider else label


def clean_variant(value):
    value = value.strip() if value else ""
    if len(value) > 50 or any(not char.isprintable() for char in value):
        raise AnalyticsError("Use a resume variant label of at most 50 printable characters.")
    presets = {"standard": "Standard", "tailored": "Tailored", "unrecorded": UNRECORDED}
    return presets.get(value.casefold(), value) or None


def application(db, job_id):
    return db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def record_application(db, job, applied_at, variant=None, response_at=None):
    score = job["score"] if job["score_method"] == "gemini" and job["scored_at"] else None
    if score is not None and date(job["scored_at"]) > date(applied_at):
        score = None
    db.execute(
        """INSERT OR IGNORE INTO applications(job_id, applied_at, role, source, score, variant, response_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job["id"], applied_at, role_type(job["title"]), source_for(db, job["id"], applied_at),
         score, variant or UNRECORDED, response_at),
    )


def update_variant(db, job_id, value):
    value = clean_variant(value)
    if value is None:
        raise AnalyticsError("Enter a resume variant or use Unrecorded.")
    row = application(db, job_id)
    if row is None:
        raise AnalyticsError("Record the application as applied first.")
    if row["variant"] == value:
        return False
    db.execute("UPDATE applications SET variant = ? WHERE job_id = ?", (value, job_id))
    return True


def record_reply(db, job_id, response_at, *, require_application=True):
    updated = db.execute(
        "UPDATE applications SET response_at = ? WHERE job_id = ? AND response_at IS NULL", (response_at, job_id),
    )
    if updated.rowcount == 0 and require_application and application(db, job_id) is None:
        raise AnalyticsError("Record the application as applied before logging its reply.")
    return updated.rowcount == 1


def backfill(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            return
        rows = db.execute(
            """SELECT jobs.*, applied.changed_at AS applied_at,
               (SELECT reply.changed_at FROM status_history AS reply
                WHERE reply.job_id = jobs.id AND reply.id > applied.id AND reply.status IN (?, ?, ?)
                ORDER BY reply.id LIMIT 1) AS response_at
               FROM jobs JOIN status_history AS applied ON applied.id = (
                   SELECT id FROM status_history WHERE job_id = jobs.id AND status = 'applied' ORDER BY id LIMIT 1
               )
               WHERE NOT EXISTS (SELECT 1 FROM applications WHERE job_id = jobs.id)""",
            RESPONSE_STATUSES,
        ).fetchall()
        for job in rows:
            record_application(db, job, job["applied_at"], response_at=job["response_at"])
        db.execute("PRAGMA user_version = 2")


def response_rate(db):
    rows = db.execute("SELECT role, source, score, variant, response_at FROM applications ORDER BY job_id").fetchall()
    groups = {field: {} for field, _ in DIMENSIONS}
    responses = 0
    for row in rows:
        responded = int(row["response_at"] is not None)
        responses += responded
        for field, _ in DIMENSIONS:
            label = score_band(row["score"]) if field == "score_band" else row[field]
            # Preserve the first recorded spelling for custom variants.
            bucket = groups[field].setdefault(label.casefold(), [label, 0, 0])
            bucket[1] += 1
            bucket[2] += responded
    return {
        "applications": len(rows),
        "responses": responses,
        "groups": {
            field: sorted(items.values(), key=(lambda item: SCORE_BANDS.index(item[0])) if field == "score_band"
                          else lambda item: (-item[1], item[0].casefold()))
            for field, items in groups.items()
        },
    }


def feedback_counts(db, now=None):
    cutoff = timestamp((now or datetime.now(timezone.utc)) - timedelta(days=MATURITY_DAYS))
    data = db.execute(
        """SELECT role, COUNT(*) AS applications, COUNT(response_at) AS responses
           FROM applications WHERE julianday(applied_at) <= julianday(?) GROUP BY role HAVING COUNT(*) >= ?""",
        (cutoff, MIN_SAMPLE),
    )
    return {row["role"]: {"applications": row["applications"], "responses": row["responses"]} for row in data}


def feedback_for(counts, title):
    role = role_type(title)
    if role not in counts:
        return None
    return {"role_type": role, **counts[role], "minimum_age_days": MATURITY_DAYS}
