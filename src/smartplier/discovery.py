"""Import, deduplicate and pre-filter jobs on demand or at a local interval."""

import hashlib
from contextlib import nullcontext
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from urllib.parse import parse_qsl

from smartplier.discovery_mail import alert_link, gmail_batch
from smartplier.discovery_sources import ALERT_DOMAINS, BOARDS, SECRET_PARTS, SourceError, adzuna_batch, ats_batch, clean_url, rss_batch, web_url
from smartplier.scoring import eligibility, has_phrase
from smartplier.tracker import add_job, database, get_job, save_score


ROLE_ALIASES = (
    "ml engineer", "ai developer", "python engineer", "backend engineer", "back-end engineer", "backend software engineer",
    "data analytics engineer", "mlops engineer", "junior developer", "graduate engineer", "software development engineer",
    "sde", "computer vision developer", "business intelligence analyst", "applied scientist",
)


def bounded(options, key, default, minimum, maximum):
    value = options.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Discovery setting {key} must be an integer from {minimum} to {maximum}.")
    return value


def load_options(path):
    return validate_options(json.loads(path.read_text(encoding="utf-8-sig")))


def validate_options(config):
    if not isinstance(config, dict):
        raise ValueError("Discovery configuration must be a JSON object.")
    companies = config.get("companies", [])
    feeds = config.get("rss", [])
    adzuna = config.get("adzuna", {})
    gmail = config.get("gmail", {})
    if not isinstance(companies, list) or not isinstance(feeds, list) or not isinstance(adzuna, dict) or not isinstance(gmail, dict):
        raise ValueError("Discovery expects companies and rss lists and adzuna and gmail objects.")
    for entry in companies:
        if (not isinstance(entry, dict) or entry.get("provider") not in BOARDS
                or not isinstance(entry.get("board"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", entry["board"])
                or not isinstance(entry.get("company"), str) or not entry["company"].strip()):
            raise ValueError("Each company needs a supported provider, a board slug, and a company name.")
    for entry in feeds:
        url = web_url(entry.get("url"), https_only=True) if isinstance(entry, dict) else None
        if (not url or not isinstance(entry.get("name"), str) or not entry["name"].strip()
                or any(name.casefold() in SECRET_PARTS for name, _ in parse_qsl(url.query, max_num_fields=120))):
            raise ValueError("Each RSS feed needs a name and a public HTTPS URL without credentials.")
    queries = adzuna.get("queries", [])
    if (not isinstance(queries, list) or len(queries) > 12
            or any(not isinstance(query, str) or not 1 <= len(query.strip()) <= 120 for query in queries)):
        raise ValueError("Adzuna queries must be a list of up to twelve short search terms.")
    adzuna["queries"] = queries
    adzuna["pages"] = bounded(adzuna, "pages", 1, 1, 5)
    adzuna["results_per_page"] = bounded(adzuna, "results_per_page", 20, 1, 50)
    gmail["days"] = bounded(gmail, "days", 14, 1, 60)
    gmail["limit"] = bounded(gmail, "limit", 50, 1, 250)
    mailbox = gmail.get("mailbox", "INBOX")
    if not isinstance(mailbox, str) or not re.fullmatch(r"[ a-zA-Z0-9_./-]{1,100}", mailbox):
        raise ValueError("Use an ASCII Gmail mailbox or label name.")
    gmail["mailbox"] = mailbox
    for section, key in ((adzuna, "enabled"), (gmail, "enabled"), (config, "filter_target_roles")):
        if key in section and type(section[key]) is not bool:
            raise ValueError(f"Discovery setting {key} must be true or false.")
    return companies, feeds, adzuna, gmail, config.get("filter_target_roles", True)


def iso(date):
    return date.isoformat(timespec="seconds") if date is not None else None


def dates(row):
    return {
        key: datetime.fromisoformat(row[key])
        for key in ("first_seen", "last_seen", "published_at", "expires_at")
        if row[key] is not None
    }


def discovery_summary(row, now=None):
    if row is None:
        return None
    now = now or datetime.now(timezone.utc)
    seen = dates(row)
    flags = []
    if seen.get("expires_at") and seen["expires_at"] <= now:
        flags.append("A source’s stated expiry date has passed.")
    if seen.get("published_at"):
        age = (now - seen["published_at"]).days
        if age >= 30:
            flags.append(f"The source’s posting date is {age} days old.")
    if row["reposts"]:
        flags.append(f"Possible reposts detected: {row['reposts']}.")
    return {
        "source": row["source"], "first_seen": seen["first_seen"].date().isoformat(),
        "last_seen": seen["last_seen"].date().isoformat(),
        "published": seen["published_at"].date().isoformat() if "published_at" in seen else None,
        "flags": flags,
    }


def job_discovery(db, job_id):
    return discovery_summary(db.execute("SELECT * FROM job_discovery WHERE job_id = ?", (job_id,)).fetchone())


def url_identity(url):
    for source in ALERT_DOMAINS:
        alert = alert_link(url, source)
        if alert:
            return f"alert:{source}:{alert[0]}"
    return clean_url(url)


def compact(value):
    return " ".join(re.findall(r"\w+", value.casefold()))


def aliases(source, post):
    external = f"id:{source}:{post.external_id}" if post.external_id else ""
    url = url_identity(post.url)
    # Require strong evidence for cross-source duplicates that have different URLs.
    exact = ""
    if len(post.description) >= 100 and post.company != "Unknown company" and post.location:
        content = "\x1f".join(compact(value) for value in (post.company, post.title, post.location, post.description))
        exact = "text:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return external, list(dict.fromkeys(filter(None, (external, "url:" + url if url else "", exact))))


def existing_alias(db, candidates):
    for alias in candidates:
        found = db.execute("SELECT job_id FROM discovery_aliases WHERE alias = ?", (alias,)).fetchone()
        if found:
            return found["job_id"]
    return None


def target_title(title, profile):
    roles = profile["preferences"]["target_roles"]
    return not roles or any(has_phrase(title, role) for role in (*roles, *ROLE_ALIASES))


def import_batch(db, batch, profile, filter_roles=True, now=None):
    now = now or datetime.now(timezone.utc)
    when = iso(now)
    counts = Counter(skipped=batch.skipped)
    own_transaction = not db.in_transaction
    with db if own_transaction else nullcontext():
        if own_transaction:
            db.execute("BEGIN IMMEDIATE")
        manual = {
            url_identity(row["url"]): row["id"]
            for row in db.execute("""SELECT id, url FROM jobs WHERE url <> '' AND NOT EXISTS
                                     (SELECT 1 FROM job_discovery WHERE job_id = jobs.id) ORDER BY id DESC""")
        }
        manual.pop("", None)
        for post in batch.postings:
            external, candidates = aliases(batch.key, post)
            if not candidates:
                counts["skipped"] += 1
                continue
            job_id = existing_alias(db, candidates) or manual.get(url_identity(post.url))
            if job_id is None:
                if filter_roles and not target_title(post.title, profile):
                    counts["off_target"] += 1
                    continue
                job_id = add_job(db, post.title, post.company, post.location, post.description, post.url)
                job = get_job(db, job_id)
                _, decision = eligibility(job, profile)
                if decision:
                    save_score(db, job, decision)
                    counts[decision.status] += 1
                counts["added"] += 1
            else:
                counts["duplicates"] += 1
            record_sighting(db, job_id, batch, post, external, candidates, when)
    return counts


def record_sighting(db, job_id, batch, post, external, candidates, when):
    published, expires = post.published_at, post.expires_at
    row = db.execute("SELECT * FROM job_discovery WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        db.execute("""INSERT INTO job_discovery(job_id, source, first_seen, last_seen, published_at, expires_at)
                      VALUES (?, ?, ?, ?, ?, ?)""",
                   (job_id, batch.label, when, when, iso(published), iso(expires)))
    else:
        previous = dates(row)
        old_date = previous.get("published_at")
        newer_date = published is not None and old_date is not None and published >= old_date + timedelta(days=1)
        known_external = existing_alias(db, [external]) if external else None
        previous_ids = db.execute("SELECT alias FROM discovery_aliases WHERE job_id = ?", (job_id,)).fetchall()
        different_id = external and not known_external and any(
            entry["alias"].startswith(f"id:{batch.key}:") for entry in previous_ids
        )
        is_repost = (newer_date and batch.label == row["source"]) or different_id
        db.execute("""UPDATE job_discovery SET last_seen = max(last_seen, ?), published_at = ?, expires_at = ?,
                      reposts = reposts + ? WHERE job_id = ?""",
                   (when, iso(published if old_date is None or is_repost and newer_date else old_date),
                    iso(expires or previous.get("expires_at")), int(bool(is_repost)), job_id))
    db.executemany("INSERT OR IGNORE INTO discovery_aliases(alias, job_id) VALUES (?, ?)",
                   ((candidate, job_id) for candidate in candidates))


def configured_sources(options, only):
    companies, feeds, adzuna, gmail, _ = options
    if only in (None, "ats"):
        for entry in companies:
            if entry.get("enabled", True):
                yield f"{entry['company']} ({entry['provider']})", partial(ats_batch, entry)
    if only in (None, "rss"):
        for entry in feeds:
            if entry.get("enabled", True):
                yield entry["name"], partial(rss_batch, entry)
    if only == "adzuna" or only is None and adzuna.get("enabled"):
        yield "Adzuna India", partial(run_adzuna, adzuna)
    if only == "gmail" or only is None and gmail.get("enabled"):
        yield "Gmail job alerts", partial(run_gmail, gmail)


def run_adzuna(options):
    app_id, app_key = (os.environ.get(name, "").strip() for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"))
    if not app_id or not app_key:
        raise SourceError("Set ADZUNA_APP_ID and ADZUNA_APP_KEY in the environment.")
    if not options["queries"]:
        raise SourceError("Add at least one Adzuna search query to the discovery configuration.")
    return adzuna_batch(options, app_id, app_key)


def run_gmail(options):
    user, password = (os.environ.get(name, "").strip() for name in ("GMAIL_USER", "GMAIL_APP_PASSWORD"))
    if not user or not password:
        raise SourceError("Set GMAIL_USER and GMAIL_APP_PASSWORD in the environment.")
    return gmail_batch(options, user, password)


def discovery_run(args):
    options = load_options(args.config)
    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    sources = list(configured_sources(options, args.source))
    if not sources:
        raise ValueError("No matching discovery sources are configured.")
    failures = 0
    for label, fetch_source in sources:
        safe_label = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", "", label).split())
        try:
            batch = fetch_source()
        except SourceError as error:
            print(f"{safe_label}: {error}", file=sys.stderr, flush=True)
            failures += 1
            continue
        with database(args.db, args.profile) as db:
            counts = import_batch(db, batch, profile, options[-1])
        print(f"{safe_label}: {counts['added']} added, {counts['duplicates']} duplicates, "
              f"{counts['review']} need review, {counts['filtered']} filtered, "
              f"{counts['off_target']} outside target roles, {counts['skipped']} skipped.",
              flush=True)
    return int(bool(failures))


def discover(args):
    if args.interval_minutes is None:
        return discovery_run(args)
    if args.interval_minutes < 1:
        raise ValueError("The discovery interval must be at least one minute.")
    print(f"Discovery will run every {args.interval_minutes} minute(s). Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            discovery_run(args)
            time.sleep(args.interval_minutes * 60)
    except KeyboardInterrupt:
        return 0
