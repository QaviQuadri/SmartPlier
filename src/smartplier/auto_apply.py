"""Durable application execution with a one-way submission boundary."""
import hashlib
import json
import re
import os
import sqlite3
import webbrowser
from datetime import date, timedelta

from smartplier import analytics
from smartplier.ats_detection import detect
from smartplier.form_engine import NeedsReview, PolicyBlocked, apply_fields, field_plan, fill_fields, inspect_summary
from smartplier.prefill import PrefillError, lever_apply_url
from smartplier import form_recipes
from smartplier import generic_application as browser_form
from urllib.parse import urlsplit
from smartplier import confirmation_mail
from smartplier.credentials import gmail_credentials
from smartplier import freshness
from smartplier import screening
from smartplier.resume import generate_resume
from smartplier.scoring import eligibility, has_phrase
from smartplier.tracker import change_status, get_job

DEFAULTS = {"enabled": False, "threshold": 80, "daily_cap": 3, "max_age": 21, "roles": [], "locations": [],
            "screening_drafts": False, "privacy_consent": False, "autopilot": False}
SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_applications (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id), identity TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
 attempted_day TEXT, submitted_at TEXT, confirmation TEXT, application_url TEXT,
 resume_version TEXT, updated_at TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_events (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_freshness_override (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id), reason TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_preflight (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id), summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_attempt_log (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id), reserved_at TEXT NOT NULL, recipe TEXT,
 snapshot TEXT NOT NULL, confirmation_state TEXT NOT NULL DEFAULT 'pending', checked_at TEXT,
 email_reference TEXT, confirmation_url TEXT
);
CREATE TABLE IF NOT EXISTS auto_diagnostics (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), at TEXT NOT NULL,
 phase TEXT NOT NULL, page_html TEXT, screenshot BLOB
);
"""


def settings(db):
    row = db.execute("SELECT value FROM web_settings WHERE key='auto_apply'").fetchone()
    return {**DEFAULTS, **(json.loads(row[0]) if row else {})}


def save_settings(db, raw):
    config = {key: raw.get(key, default) for key, default in DEFAULTS.items()}
    for key, minimum, maximum in (("threshold", 0, 100), ("daily_cap", 1, 25), ("max_age", 1, 90)):
        if type(config[key]) is not int or not minimum <= config[key] <= maximum:
            raise ValueError(f"Invalid Auto-Apply setting: {key}.")
    if type(config["enabled"]) is not bool or any(type(config[key]) is not bool for key in ("screening_drafts", "privacy_consent", "autopilot")) or any(
        not isinstance(config[key], list) or len(config[key]) > 50
        or any(not isinstance(value, str) or not value.strip() or len(value) > 100 for value in config[key])
        for key in ("roles", "locations")
    ):
        raise ValueError("Enter valid role and location filters.")
    if config["autopilot"] and not config["enabled"]:
        raise ValueError("Enable Auto-Apply before enabling scheduled submission.")
    with db:
        previous = settings(db)
        if config["autopilot"] and not previous["autopilot"] and raw.get("authorize_autopilot") is not True:
            raise ValueError("Confirm standing authorization for automatic submissions.")
        db.execute("INSERT OR REPLACE INTO web_settings VALUES ('auto_apply',?)", (json.dumps(config),))


def update(db, job, status, detail):
    stamp = analytics.timestamp()
    db.execute("UPDATE auto_applications SET status=?,detail=?,updated_at=? WHERE job_id=?",
               (status, detail, stamp, job))
    db.execute("INSERT INTO auto_events(job_id,at,status,detail) VALUES (?,?,?,?)",
               (job, stamp, status, detail))


def recover(db):
    with db:
        for row in db.execute("SELECT job_id,attempted_day FROM auto_applications WHERE status IN ('queued','processing')").fetchall():
            text = ("Interrupted after the submission boundary. Check the ATS before taking further action."
                    if row["attempted_day"] else "Interrupted before submission.")
            if row["attempted_day"]:
                db.execute("UPDATE auto_attempt_log SET confirmation_state='uncertain' WHERE job_id=? AND confirmation_state='pending'",
                           (row["job_id"],))
            update(db, row["job_id"], "needs_review", text)
        update_circuit(db)


def report(db):
    return {"settings": settings(db), "circuit": circuit(db), "digest": digest(db),
            "rows": [dict(row) for row in db.execute(
                """SELECT a.*, j.title,j.company FROM auto_applications a JOIN jobs j ON j.id=a.job_id
                   ORDER BY a.updated_at DESC,a.job_id DESC""")],
            "history": [dict(row) for row in db.execute("SELECT * FROM auto_events ORDER BY id DESC LIMIT 150")]}


def recorded_preflight(db, ident):
    row = db.execute("SELECT summary FROM auto_preflight WHERE job_id=?", (ident,)).fetchone()
    return json.loads(row[0]) if row else None


def freshness_override(db, ident):
    row = db.execute("SELECT reason,recorded_at FROM auto_freshness_override WHERE job_id=?", (ident,)).fetchone()
    return dict(row) if row else None


def save_override(db, ident, reason):
    get_job(db, ident)
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("Enter a freshness override reason of 1–500 characters.")
    with db:
        db.execute("INSERT OR REPLACE INTO auto_freshness_override VALUES (?,?,?)", (ident, reason, analytics.timestamp()))
        db.execute("DELETE FROM auto_preflight WHERE job_id=?", (ident,))
        db.execute("INSERT INTO auto_events(job_id,at,status,detail) VALUES (?,?,?,?)",
                   (ident, analytics.timestamp(), "freshness_override", reason))


def matches(job, config, profile):
    if job["status"] not in ("to_apply", "unscored", "low"):
        return False
    if eligibility(job, profile)[1]:
        return False
    if job["score_method"] and (job["score_method"] != "gemini" or job["score"] < config["threshold"]):
        return False
    if job["score_details"] and json.loads(job["score_details"]).get("ghost_job_risk") == "high":
        return False
    for category, field in (("roles", "title"), ("locations", "location")):
        terms = config[category] or (profile["preferences"]["target_roles"] if category == "roles" else [])
        if terms and not any(has_phrase(job[field], word) for word in terms):
            return False
    return bool(browser_form.origin(job["url"]))


def queue(work, *, scheduled=False):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        config = settings(db)
        if not config["enabled"]:
            raise ValueError("Enable Auto-Apply in its settings first.")
        used = db.execute("SELECT COUNT(*) FROM auto_applications WHERE attempted_day=?", (date.today().isoformat(),)).fetchone()[0]
        remaining = config["daily_cap"] - used
        if remaining <= 0:
            raise ValueError("The daily submission cap has been reached.")
        profile = work.profile_data()
        candidates = []
        jobs = db.execute("""SELECT j.* FROM jobs j WHERE NOT EXISTS(SELECT 1 FROM applications a WHERE a.job_id=j.id)
                          AND NOT EXISTS(SELECT 1 FROM auto_applications a WHERE a.job_id=j.id)
                          ORDER BY j.score DESC,j.id DESC""").fetchall()
        reserved = {row[0] for row in db.execute("SELECT identity FROM auto_applications")}
        applied = [row[0] for row in db.execute(
            "SELECT j.url FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.url<>''")]
        applied_identities = {posting_identity(url) for url in applied}
        for job in jobs:
            if not matches(job, config, profile):
                continue
            identity = posting_identity(job["url"])
            if not identity or identity in reserved or identity in applied_identities:
                continue
            reserved.add(identity)
            candidates.append((job["id"], identity))
            if len(candidates) >= remaining:
                break
        if not candidates:
            raise ValueError("No eligible new applications were found.")
        for ident, identity in candidates:
            db.execute("INSERT INTO auto_applications(job_id,identity,status,updated_at,detail) VALUES (?,?,?,?,?)",
                       (ident, identity,"queued",analytics.timestamp(),"Queued for verification."))
            update(db, ident, "queued", "Queued for verification.")
        # The worker opens its own connection after this transaction commits.
        return work.queue("auto-apply", "Prepare Auto-Apply preflights", [(f"Job #{ident}", ident) for ident, _ in candidates],
                          lambda ident, _run: preflight(work, ident, scheduled=scheduled))


def posting_identity(url):
    try:
        return lever_apply_url(url).casefold()
    except PrefillError:
        return url.split("?")[0].rstrip("/").casefold()


def verify_freshness(db, job, config):
    source = db.execute("SELECT * FROM job_discovery WHERE job_id=?", (job["id"],)).fetchone()
    now = analytics.date(analytics.timestamp())
    try:
        if source and source["expires_at"] and analytics.date(source["expires_at"]) <= now:
            raise PolicyBlocked("The posting is explicitly expired.")
        published = analytics.date(source["published_at"]) if source and source["published_at"] else None
    except (ValueError, TypeError) as error:
        if isinstance(error, PolicyBlocked):
            raise
        raise NeedsReview("The posting date is ambiguous.") from None
    if not published or published > now:
        raise NeedsReview("The posting has no unambiguous publication date.")
    if source["reposts"]:
        raise NeedsReview("The posting has a repost signal and needs review.")
    return (now - published).days


def related_title(title):
    return " ".join(re.findall(r"\w+", re.sub(r"^hiring\s+", "", title.strip(), flags=re.I).casefold()))


def duplicates(db, job, identity):
    known = db.execute("""SELECT j.url,j.title,j.company FROM jobs j WHERE j.id<>? AND
        (EXISTS(SELECT 1 FROM applications a WHERE a.job_id=j.id)
        OR EXISTS(SELECT 1 FROM auto_applications a WHERE a.job_id=j.id AND a.attempted_day IS NOT NULL))""", (job["id"],))
    return any(posting_identity(row["url"]) == identity or
               row["company"].strip().casefold() == job["company"].strip().casefold() and related_title(row["title"]) == related_title(job["title"])
               for row in known)


def input_version(job, profile, config, answers):
    evidence = [job["revision"], job["url"], profile, {k: v for k, v in config.items() if k != "enabled"}, answers]
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def active_posting(work, job):
    with work.connection() as db:
        aliases = [row[0] for row in db.execute("SELECT alias FROM discovery_aliases WHERE job_id=?", (job["id"],))]
    return freshness.verify(job, aliases)


def single_row(db, job):
    identity = posting_identity(job["url"])
    row = db.execute("SELECT * FROM auto_applications WHERE job_id=?", (job["id"],)).fetchone()
    if not identity or duplicates(db, job, identity) or db.execute("SELECT 1 FROM applications WHERE job_id=?", (job["id"],)).fetchone():
        raise PolicyBlocked("This or a matching employer role already has an application or submission attempt.")
    if row and row["attempted_day"]:
        raise PolicyBlocked("An earlier submission attempt exists. Check the ATS before taking further action.")
    if db.execute("SELECT 1 FROM auto_applications WHERE identity=? AND job_id<>?", (identity, job["id"])).fetchone():
        raise PolicyBlocked("Another tracked job has the same ATS posting ID.")
    if not row:
        db.execute("INSERT INTO auto_applications(job_id,identity,status,updated_at,detail) VALUES (?,?,?,?,?)",
                   (job["id"], identity, "queued", analytics.timestamp(), "Queued for preflight."))
    update(db, job["id"], "queued", "Queued for preflight.")


def queue_single(work, ident, *, submit=False):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        job = get_job(db, ident)
        if submit:
            check_preflight(db, job, work.profile_data())
        else:
            single_row(db, job)
        return work.queue("auto-apply", "Submit selected application" if submit else "Selected job preflight",
                          [(f"Job #{ident}", ident)],
                          lambda job_id, _run: submit_selected(work, job_id) if submit else preflight(work, job_id))


def recheck_screening(work, ident):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,attempted_day FROM auto_applications WHERE job_id=?", (ident,)).fetchone()
        if not row or row["status"] != "needs_review" or row["attempted_day"]:
            return {"status": "unchanged"}
        previous = recorded_preflight(db, ident)
        single_row(db, get_job(db, ident))
    return preflight(work, ident, scheduled=bool(previous and previous.get("scheduled")))


def launch(driver, gate, *, visible=False):
    channel = os.getenv("SMARTPLIER_BROWSER_CHANNEL")
    return driver.chromium.launch(args=gate.args, headless=not visible, **({"channel": channel} if channel else {}))


def prepared_form(page, gate, target, questions, privacy, expected=None):
    form, details, navigation = browser_form.find_form(page, gate, target, privacy)
    fields = details["fields"]
    if expected and form_recipes.digest(fields) != expected:
        raise NeedsReview("The employer form changed after preflight. Run a new preflight.")
    fingerprint, mapping = form_recipes.plan(questions.work, questions.profile, fields, navigation, "native-post")
    form_recipes.attach(fields, mapping)
    return form, details, fingerprint


def inspect_form(job, profile, html=None, observed_url=None, questions=None, *, privacy=False, expected=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise NeedsReview("Install Python Playwright and Chromium to inspect employer fields.") from None
    target = observed_url or job["url"]
    gate = browser_form.Gate(target)
    with sync_playwright() as driver, launch(driver, gate) as browser:
        context = browser.new_context(java_script_enabled=False, service_workers="block", accept_downloads=False)
        gate.install(context)
        page = context.new_page()
        try:
            form, details, fingerprint = prepared_form(page, gate, target, questions, privacy, expected)
            fields = details["fields"]
            summary = inspect_summary(form, fields, profile["basics"], profile, questions)
            summary.update(recipe=fingerprint, application_page=page.url)
            if html and not summary["unanswered"]:
                pdf = render_pdf(browser, html)
                fill_fields(form, fields, profile["basics"], profile, attachment(pdf), questions)
                if not form.evaluate("f => f.checkValidity()"):
                    raise NeedsReview("The form reports invalid screening field values.")
                summary["snapshot"] = browser_form.submission_snapshot(
                    form, details, hashlib.sha256(html.read_bytes()).hexdigest())
                summary["pdf_validated"] = True
            questions.finish()
            return summary
        except Exception:
            capture(questions.work, job["id"], page, "preflight failure")
            raise


def preflight(work, ident, *, scheduled=False):
    summary = {"at": analytics.timestamp(), "ready": False, "verified": False, "fields": [],
               "unanswered": [], "inspected": False, "resume": None, "scheduled": scheduled}
    status = "needs_review"
    try:
        with work.connection() as db, db:
            job = get_job(db, ident)
            summary["ats"] = detect(job["url"]).as_dict()
            config = settings(db)
            if scheduled and config["autopilot"] and circuit(db).get("paused"):
                raise NeedsReview("Autopilot paused. Review the circuit breaker in Auto-Apply.")
            update(db, ident, "processing", "Preparing a read-only preflight.")
            summary["age"] = verify_freshness(db, job, config)
            summary["threshold"] = config["max_age"]
            summary["override"] = freshness_override(db, ident)
        initial = detect(job["url"])
        profile = work.profile_data()
        questions = screening.Session(work, job, profile, initial.platform, plan=True)
        with work.connection() as db:
            answers = screening.revision(db)
        target = job["url"]
        if initial.platform in freshness.BOARD_CHECKS:
            verification = active_posting(work, job)
            summary["verification"] = verification.message
            summary["verified"] = True
            target = verification.application_url
            summary["board_application_url"] = target
        if not matches(job, config, profile):
            raise PolicyBlocked("The job does not meet the saved eligibility or score rules.")
        if scheduled and job["score"] is None:
            work.score(ident, None)
            with work.connection() as db:
                job = get_job(db, ident)
            if not matches(job, config, profile) or job["score_method"] != "gemini":
                raise PolicyBlocked("The Gemini score is below the Auto-Apply threshold or is unavailable.")
        summary.update(inspect_form(job, profile, observed_url=target, questions=questions, privacy=config["privacy_consent"]))
        summary["inspected"] = True
        summary["ats"] = detect(job["url"], observed_url=summary.get("application_page", target),
                                official_listing=summary["verified"], form_inspected=True).as_dict()
        if summary["unanswered"]:
            raise NeedsReview("The form has unanswered or protected mandatory fields.")
        if job["score"] is None:
            work.score(ident, None)
        with work.connection() as db:
            job = get_job(db, ident)
            if not matches(job, config, profile) or job["score_method"] != "gemini":
                raise PolicyBlocked("The Gemini score is below the Auto-Apply threshold or is unavailable.")
            paths, warnings, _ = generate_resume(db, work.db, work.profile, ident, work.provider)
        summary["resume"] = hashlib.sha256(paths[0].read_bytes()).hexdigest()
        summary["warnings"] = warnings
        if summary["age"] > config["max_age"]:
            if not summary["verified"]:
                raise NeedsReview("This older posting requires official board verification.")
            if not summary["override"]:
                raise NeedsReview("The posting is active but above the stale-risk threshold. Record a reason to proceed.")
        summary.update(inspect_form(job, profile, paths[0], observed_url=target, questions=questions,
                                    privacy=config["privacy_consent"], expected=summary.get("recipe")))
        if summary["unanswered"]:
            raise NeedsReview("The form has unanswered or protected mandatory fields.")
        summary["ready"] = True
        summary["input"] = input_version(job, profile, config, answers)
        summary["answer_revision"] = answers
        summary["screening_keys"] = sorted(questions.used)
        if scheduled and config["autopilot"] and summary.get("snapshot"):
            summary["autopilot_due"] = analytics.timestamp(analytics.date(analytics.timestamp()) + timedelta(minutes=3))
        status = "ready"
        detail = "Preflight ready. Review the summary and submit this selected job."
    except PolicyBlocked as error:
        status, detail = "blocked", str(error)
    except NeedsReview as error:
        detail = str(error)
    except Exception as error:
        status, detail = "failed", f"Preflight could not finish ({type(error).__name__}). No submission was attempted."
    summary["detail"] = detail
    with work.connection() as db, db:
        db.execute("INSERT OR REPLACE INTO auto_preflight VALUES (?,?)", (ident, json.dumps(summary)))
        update(db, ident, status, detail)
        if status == "blocked":
            update_circuit(db)
    return {"status": status, "detail": detail}


def check_preflight(db, job, profile):
    config = settings(db)
    if not config["enabled"]:
        raise ValueError("Enable Auto-Apply before submitting this selected job.")
    summary = recorded_preflight(db, job["id"])
    row = db.execute("SELECT * FROM auto_applications WHERE job_id=?", (job["id"],)).fetchone()
    if (not summary or not summary["ready"] or not row or row["status"] != "ready"
            or analytics.date(analytics.timestamp()) - analytics.date(summary["at"]) > timedelta(minutes=15)
            or summary.get("input") != input_version(job, profile, config, screening.revision(db))):
        raise ValueError("Run a fresh preflight before submitting this job.")
    if row["attempted_day"] or db.execute("SELECT 1 FROM applications WHERE job_id=?", (job["id"],)).fetchone() or duplicates(db, job, row["identity"]):
        raise PolicyBlocked("This or a matching employer role already has an application or submission attempt.")
    if verify_freshness(db, job, config) > config["max_age"] and not freshness_override(db, job["id"]):
        raise NeedsReview("Record a freshness override for this active older posting.")
    return summary


def submit_selected(work, ident, *, scheduled=False):
    with work.connection() as db:
        job = get_job(db, ident)
        summary = check_preflight(db, job, work.profile_data())
        if scheduled:
            require_autopilot(db, summary)
    try:
        if detect(job["url"]).platform in freshness.BOARD_CHECKS:
            verification = active_posting(work, job)
            if summary.get("board_application_url") and verification.application_url != summary["board_application_url"]:
                raise NeedsReview("The official application address changed after preflight.")
    except NeedsReview as error:
        with work.connection() as db, db:
            update(db, ident, "blocked" if isinstance(error, PolicyBlocked) else "needs_review", str(error))
            if isinstance(error, PolicyBlocked):
                update_circuit(db)
        return {"status": "blocked" if isinstance(error, PolicyBlocked) else "needs_review", "detail": str(error)}
    return execute(work, ident, expected_resume=summary["resume"], scheduled=scheduled)


def reserve(work, ident, url, version, expected_profile=None, expected_answers=None, snapshot=None, recipe=None, *, scheduled=False, assisted=False):
    with work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        config = settings(db)
        if not config["enabled"]:
            raise NeedsReview("Auto-Apply was disabled before submission.")
        today = date.today().isoformat()
        if db.execute("SELECT COUNT(*) FROM auto_applications WHERE attempted_day=?", (today,)).fetchone()[0] >= config["daily_cap"]:
            raise NeedsReview("The daily submission cap has been reached.")
        row = db.execute("SELECT attempted_day,identity FROM auto_applications WHERE job_id=?", (ident,)).fetchone()
        job = get_job(db, ident)
        if row["attempted_day"] or db.execute("SELECT 1 FROM applications WHERE job_id=?", (ident,)).fetchone() or duplicates(db, job, row["identity"]):
            raise PolicyBlocked("This or a matching employer role already has an application or submission attempt.")
        current_profile = work.profile_data()
        if expected_profile is not None and current_profile != expected_profile:
            raise NeedsReview("The profile was edited while the application form was being filled.")
        if expected_answers is not None and screening.revision(db) != expected_answers:
            raise NeedsReview("Screening answers changed after preflight. Review the job again.")
        if snapshot is not None and not assisted:
            preflight_record = recorded_preflight(db, ident)
            if (not preflight_record or preflight_record.get("snapshot") != snapshot
                    or preflight_record.get("input") != input_version(job, current_profile, config, expected_answers)):
                raise NeedsReview("The approved form or application settings changed. Run a new preflight.")
            if scheduled:
                require_autopilot(db, preflight_record)
        if not matches(job, config, current_profile) or job["score_method"] != "gemini":
            raise NeedsReview("The posting no longer meets the saved Auto-Apply rules.")
        if verify_freshness(db, job, config) > config["max_age"] and not freshness_override(db, ident):
            raise NeedsReview("Record a freshness override for this active older posting.")
        db.execute("UPDATE auto_applications SET attempted_day=?,application_url=?,resume_version=? WHERE job_id=?",
                   (today,url,version,ident))
        if snapshot is not None:
            db.execute("INSERT INTO auto_attempt_log(job_id,reserved_at,recipe,snapshot) VALUES (?,?,?,?)",
                       (ident, analytics.timestamp(), recipe, json.dumps(snapshot)))
            for _ in snapshot["consents"]:
                db.execute("INSERT INTO auto_events(job_id,at,status,detail) VALUES (?,?,?,?)",
                           (ident, analytics.timestamp(), "consent", "Used the standing privacy/data-processing policy."))
        update(db, ident, "processing", "Submission boundary reached. Automatic retries are disabled.")


def render_pdf(browser, html):
    context = browser.new_context(offline=True, java_script_enabled=False, service_workers="block")
    try:
        page = context.new_page()
        page.set_content(html.read_text(encoding="utf-8"), wait_until="load")
        page.emulate_media(media="print")
        for scale in (1, .96, .92):
            pdf = page.pdf(prefer_css_page_size=True, print_background=True, scale=scale)
            if len(re.findall(rb"/Type\s*/Page\b", pdf)) == 1:
                return pdf
        raise NeedsReview("The generated resume PDF is not one page. Review it in Resume Studio.")
    finally:
        context.close()


def attachment(pdf):
    return {"name": "Resume.pdf", "mimeType": "application/pdf", "buffer": pdf}


def capture(work, ident, page, phase):
    html, image = None, None
    try:
        html = page.evaluate("""() => {
          const copy = document.documentElement.cloneNode(true);
          copy.querySelectorAll('script,iframe,object,embed').forEach(element => element.remove());
          copy.querySelectorAll('input').forEach(input => {
            if (['hidden','password'].includes(input.type) ||
                /password|csrf|token|secret|session|authenticity/i.test(input.name)) input.removeAttribute('value');
          });
          copy.querySelectorAll('*').forEach(el => [...el.attributes].forEach(attr => {
            if (/^on|csrf|token|secret|session|nonce|authenticity/i.test(attr.name)) el.removeAttribute(attr.name);
          }));
          return copy.outerHTML.slice(0,2000000);
        }""")
    except Exception:
        pass
    try:
        image = page.screenshot(timeout=4000)
    except Exception:
        pass
    try:
        with work.connection() as db, db:
            db.execute("INSERT INTO auto_diagnostics(job_id,at,phase,page_html,screenshot) VALUES (?,?,?,?,?)",
                       (ident, analytics.timestamp(), phase, html, image))
    except (OSError, ValueError, sqlite3.Error):
        pass


def deliver(work, job, profile, html, *, scheduled=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise NeedsReview("Install Python Playwright and Chromium to enable submission.") from None
    with work.connection() as db:
        summary = recorded_preflight(db, job["id"])
        answers = screening.revision(db)
        privacy = settings(db)["privacy_consent"]
    if not summary or summary.get("answer_revision") != answers or not summary.get("snapshot") or not summary.get("recipe"):
        raise NeedsReview("A current, reviewed payload snapshot is required.")
    target = summary["application_page"]
    gate = browser_form.Gate(target)
    questions = screening.Session(work, job, profile, detect(job["url"]).platform, plan=False,
                                  approved=set(summary.get("screening_keys", [])))
    with sync_playwright() as driver, launch(driver, gate) as browser:
        pdf = render_pdf(browser, html)
        version = hashlib.sha256(pdf).hexdigest()
        context = browser.new_context(java_script_enabled=False, service_workers="block", accept_downloads=False)
        gate.install(context)
        page = context.new_page()
        try:
            form, details, fingerprint = prepared_form(page, gate, target, questions, privacy, summary["recipe"])
            fill_fields(form, details["fields"], profile["basics"], profile, attachment(pdf), questions)
            snapshot = browser_form.submission_snapshot(form, details, hashlib.sha256(html.read_bytes()).hexdigest())
            if snapshot != summary["snapshot"]:
                raise NeedsReview("The form payload differs from the approved preflight. Run a new preflight.")
            capture(work, job["id"], page, "before submission")
            result = browser_form.submit_form(page, gate, form, details, snapshot,
                lambda url, payload: reserve(work, job["id"], url, version, profile, answers, payload, fingerprint, scheduled=scheduled))
            capture(work, job["id"], page, result["state"])
            return result, version
        except Exception:
            capture(work, job["id"], page, "submission failure")
            raise


def execute(work, ident, expected_resume=None, *, scheduled=False):
    phase = "eligibility check"
    try:
        with work.connection() as db, db:
            update(db, ident, "processing", "Checking eligibility and the employer posting.")
            job = get_job(db, ident)
            config = settings(db)
            verify_freshness(db, job, config)
        profile = work.profile_data()
        if not config["enabled"] or not matches(job, config, profile):
            raise NeedsReview("The job no longer meets Auto-Apply rules.")
        if job["score"] is None:
            phase = "Gemini scoring"
            work.score(ident, None)
        phase = "tailored resume generation"
        with work.connection() as db:
            job = get_job(db, ident)
            if not matches(job, config, profile) or job["score_method"] != "gemini":
                raise NeedsReview("The Gemini score is below the Auto-Apply threshold or is unavailable.")
            with db:
                update(db, ident, "processing", "Eligibility and Gemini score verified. Preparing the tailored resume.")
            files, _, _ = generate_resume(db, work.db, work.profile, ident, work.provider)
            if expected_resume and hashlib.sha256(files[0].read_bytes()).hexdigest() != expected_resume:
                raise NeedsReview("The tailored resume changed since preflight. Run a fresh preflight.")
            with db:
                update(db, ident, "processing", "Tailored resume prepared. Inspecting the employer form.")
        phase = "browser application"
        outcome, version = deliver(work, job, profile, files[0], **({"scheduled": True} if scheduled else {}))
        phase = "confirmation recording"
        return record_outcome(work, ident, outcome, version)
    except Exception as error:
        with work.connection() as db, db:
            attempt = db.execute("SELECT attempted_day FROM auto_applications WHERE job_id=?", (ident,)).fetchone()
            crossed = bool(attempt and attempt[0])
            status = ("needs_review" if crossed else "blocked" if isinstance(error, PolicyBlocked)
                      else "needs_review" if isinstance(error, NeedsReview) else "failed")
            detail = (str(error) if isinstance(error, NeedsReview) else
                      f"Failed during {phase} ({type(error).__name__}).")
            if crossed:
                attempt_log = db.execute("SELECT recipe FROM auto_attempt_log WHERE job_id=?", (ident,)).fetchone()
                db.execute("UPDATE auto_attempt_log SET confirmation_state='uncertain' WHERE job_id=?", (ident,))
                if attempt_log:
                    form_recipes.outcome(db, attempt_log["recipe"], False)
                detail += " Submission may have occurred. Check the ATS before taking further action."
            update(db, ident, status, detail)
            if crossed or status == "blocked":
                update_circuit(db)
        raise ValueError(detail) from None


def _confirmed(db, ident, message, url, version, at=None):
    at = at or analytics.timestamp()
    db.execute("""UPDATE auto_applications SET confirmation=?,submitted_at=?,resume_version=?,
                  application_url=COALESCE(application_url,?) WHERE job_id=?""", (message, at, version, url, ident))
    change_status(db, ident, "applied", "Confirmed by the employer.", "Tailored " + version[:12])
    update(db, ident, "submitted", "Employer application confirmation verified.")


def reconcile_receipts(work):
    now = analytics.date(analytics.timestamp())
    with work.connection() as db:
        rows = [dict(row) for row in db.execute("""SELECT l.*, a.confirmation,a.resume_version,a.application_url,
                    j.company,j.title,j.url FROM auto_attempt_log l JOIN auto_applications a USING(job_id)
                    JOIN jobs j ON j.id=l.job_id WHERE l.confirmation_state='probable'""")]
    if not rows:
        return {"confirmed": 0, "review": 0}
    active = [row for row in rows if now - analytics.date(row["reserved_at"]) <= timedelta(days=3)]
    receipts = {}
    if active:
        try:
            receipts = confirmation_mail.find_receipts(*gmail_credentials(), active)
        except (OSError, ValueError, confirmation_mail.imaplib.IMAP4.error):
            pass
    reviewed = verified = 0
    with work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        for row in rows:
            ident = row["job_id"]
            latest = db.execute("""SELECT l.confirmation_state,a.status,j.status FROM auto_attempt_log l
                                    JOIN auto_applications a USING(job_id) JOIN jobs j ON j.id=l.job_id
                                    WHERE l.job_id=?""", (ident,)).fetchone()
            if not latest or latest[0] != "probable":
                continue
            db.execute("UPDATE auto_attempt_log SET checked_at=? WHERE job_id=?", (analytics.timestamp(), ident))
            if latest[2] in {"rejected", "withdrawn", "closed", "interview", "offer", "applied"}:
                db.execute("UPDATE auto_attempt_log SET confirmation_state='manual' WHERE job_id=?", (ident,))
                continue
            if ident in receipts:
                db.execute("UPDATE auto_attempt_log SET confirmation_state='confirmed',email_reference=? WHERE job_id=?",
                           (receipts[ident], ident))
                _confirmed(db, ident, row["confirmation"], row["confirmation_url"] or row["application_url"],
                           row["resume_version"], row["reserved_at"])
                form_recipes.outcome(db, row["recipe"], True)
                verified += 1
            elif row not in active:
                db.execute("UPDATE auto_attempt_log SET confirmation_state='uncertain' WHERE job_id=?", (ident,))
                update(db, ident, "needs_review", "No matching receipt arrived within three days. Check the employer before retrying manually.")
                form_recipes.outcome(db, row["recipe"], False)
                reviewed += 1
        update_circuit(db)
    return {"confirmed": verified, "review": reviewed}


def record_outcome(work, ident, outcome, version):
    state = outcome["state"]
    with work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        attempt = db.execute("SELECT recipe FROM auto_attempt_log WHERE job_id=?", (ident,)).fetchone()
        if state == "confirmed":
            _confirmed(db, ident, outcome["confirmation"], outcome["url"], version)
        elif state == "probable":
            db.execute("UPDATE auto_applications SET confirmation=? WHERE job_id=?", (outcome["confirmation"], ident))
            update(db, ident, "probable", "Employer confirmation page found. Waiting for a matching receipt.")
        else:
            update(db, ident, "needs_review", "Submission may have occurred. Check the employer before taking further action.")
        db.execute("UPDATE auto_attempt_log SET confirmation_state=?,confirmation_url=? WHERE job_id=?",
                   (state, outcome["url"], ident))
        if attempt and state != "probable":
            form_recipes.outcome(db, attempt["recipe"], state == "confirmed")
        update_circuit(db)
    return {"status": {"confirmed": "submitted", "probable": "probable", "uncertain": "needs_review"}[state]}



def circuit(db):
    row = db.execute("SELECT value FROM web_settings WHERE key='autopilot_circuit'").fetchone()
    return json.loads(row[0]) if row else {"paused": False}


def update_circuit(db):
    previous = circuit(db)
    if previous["paused"]:
        return
    run = previous.get("resumed_at", "")
    attempts = [row[0] for row in db.execute(
        """SELECT confirmation_state FROM auto_attempt_log WHERE reserved_at>=?
           AND confirmation_state IN ('confirmed','probable','failed','uncertain','manual')
           ORDER BY reserved_at DESC,job_id DESC LIMIT 2""", (run,))]
    hour = analytics.timestamp(analytics.date(analytics.timestamp()) - timedelta(hours=1))
    blocks = db.execute("SELECT COUNT(DISTINCT job_id) FROM auto_events WHERE status='blocked' AND at>=?",
                        (max(hour, run),)).fetchone()[0]
    if len(attempts) == 2 and all(state in {"failed", "uncertain"} for state in attempts):
        reason = "Two consecutive submissions failed or could not be confirmed."
    elif blocks >= 3:
        reason = "Three distinct jobs hit policy blocks in the last hour."
    else:
        return
    state = {**previous, "paused": True, "at": analytics.timestamp(), "reason": reason}
    db.execute("INSERT OR REPLACE INTO web_settings VALUES ('autopilot_circuit',?)", (json.dumps(state),))


def resume_autopilot(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR REPLACE INTO web_settings VALUES ('autopilot_circuit',?)",
                   (json.dumps({"paused": False, "resumed_at": analytics.timestamp()}),))


def ramp_daily_cap(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        config = settings(db)
        if config["daily_cap"] >= 25:
            raise ValueError("The maximum daily cap is 25.")
        config["daily_cap"] += 1
        db.execute("INSERT OR REPLACE INTO web_settings VALUES ('auto_apply',?)", (json.dumps(config),))
    return config["daily_cap"]


def digest(db):
    start = analytics.timestamp(analytics.date(analytics.timestamp()) - timedelta(days=1))
    return [{"status": row[0], "count": row[1]} for row in db.execute(
        """SELECT status, COUNT(DISTINCT job_id) FROM auto_events WHERE at>=?
           AND status IN ('submitted','ready','probable','needs_review','blocked','failed')
           GROUP BY status ORDER BY status""", (start,))]


def require_autopilot(db, summary):
    cfg = settings(db)
    if not cfg["enabled"] or not cfg["autopilot"] or circuit(db)["paused"] or not summary.get("autopilot_due"):
        raise NeedsReview("Standing authorization for the scheduled submission is unavailable.")
    if analytics.date(summary["autopilot_due"]) > analytics.date(analytics.timestamp()):
        raise NeedsReview("The scheduled payload review window is still open.")


def queue_due(work):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        cfg = settings(db)
        if not cfg["enabled"] or not cfg["autopilot"] or circuit(db)["paused"]:
            return
        profile = work.profile_data()
        remaining = cfg["daily_cap"] - db.execute("SELECT count(*) FROM auto_applications WHERE attempted_day=?",
                                                 (date.today().isoformat(),)).fetchone()[0]
        if remaining <= 0:
            return
        candidates = []
        rows = db.execute("SELECT job_id FROM auto_applications WHERE status='ready' AND attempted_day IS NULL "
                          "ORDER BY updated_at,job_id").fetchall()
        for row in rows:
            ident = row[0]
            summary = recorded_preflight(db, ident)
            if not summary or not summary.get("autopilot_due"):
                continue
            try:
                require_autopilot(db, summary)
            except NeedsReview:
                continue
            try:
                check_preflight(db, get_job(db, ident), profile)
            except ValueError:
                update(db, ident, "needs_review", "Scheduled review expired or its inputs changed. Run a fresh preflight.")
                continue
            candidates.append((f"Job #{ident}", ident))
            if len(candidates) >= remaining:
                break
        if candidates:
            work.queue("auto-apply", "Scheduled application submissions", candidates,
                       lambda ident, _run: submit_selected(work, ident, scheduled=True))


def cancel_scheduled(work, ident):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        summary = recorded_preflight(db, ident)
        if not summary or not summary.pop("autopilot_due", None):
            raise ValueError("This job has no scheduled submission.")
        if db.execute("SELECT attempted_day FROM auto_applications WHERE job_id=?", (ident,)).fetchone()[0]:
            raise ValueError("The application has already crossed the submission boundary.")
        db.execute("UPDATE auto_preflight SET summary=? WHERE job_id=?", (json.dumps(summary), ident))
        db.execute("INSERT INTO auto_events(job_id,at,status,detail) VALUES (?,?,?,?)",
                   (ident, analytics.timestamp(), "cancelled", "Scheduled submission cancelled."))



def queue_assisted(work, ident):
    with work.lock, work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        job = get_job(db, ident)
        if not browser_form.origin(job["url"]):
            raise PolicyBlocked("This posting has no permitted employer application URL.")
        single_row(db, job)
        return work.queue("auto-apply", "Open assisted application", [(f"Job #{ident}", ident)],
                          lambda job_id, _run: assisted(work, job_id))


def assisted(work, ident):
    with work.connection() as db, db:
        job = get_job(db, ident)
        target = job["url"]
        summary = recorded_preflight(db, ident)
        if summary and browser_form.origin(summary.get("application_page", "")) == browser_form.origin(target):
            target = summary["application_page"]
        update(db, ident, "processing", "Opening a visible browser for assisted review.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return assisted_external(work, ident, target, "Python Playwright is unavailable.")
    try:
        gate = browser_form.Gate(target)
        with sync_playwright() as driver, launch(driver, gate, visible=True) as browser:
            context = browser.new_context(java_script_enabled=False, service_workers="block", accept_downloads=False)
            gate.install(context)
            page = context.new_page()
            try:
                form, details, navigation = browser_form.find_form(page, gate, target)
                fields = details["fields"]
                fingerprint = form_recipes.digest(fields)
                with work.connection() as db:
                    cached = db.execute("SELECT mapping FROM form_recipes WHERE fingerprint=?", (fingerprint,)).fetchone()
                if cached:
                    form_recipes.attach(fields, json.loads(cached["mapping"]))
                profile = work.profile_data()
                with work.connection() as db:
                    pages, _, _ = generate_resume(db, work.db, work.profile, ident, work.provider)
                    answers = screening.revision(db)
                pdf = render_pdf(browser, pages[0])
                version = hashlib.sha256(pdf).hexdigest()
                questions = screening.Session(work, job, profile, detect(target).platform, plan=False)
                fills, _ = field_plan(form, fields, profile["basics"], profile, attachment(pdf), questions)
                apply_fields(fills)
                old_url, old_body = page.url, page.locator("body").inner_text(timeout=5000)
                detail = "Assisted browser is open. Review and complete the form there."
                with work.connection() as db, db:
                    update(db, ident, "processing", detail)
                capture(work, ident, page, "assisted form")
                gate.endpoint = details["action"]

                def approve(request):
                    snapshot = browser_form.posted_snapshot(request, details, version)
                    reserve(work, ident, request.url, version, profile, answers, snapshot, fingerprint, assisted=True)

                gate.before_post = approve
                for _ in range(600):
                    if page.is_closed() or work.stop.is_set() or gate.sent or gate.review_error:
                        break
                    page.wait_for_timeout(1000)
                if gate.sent:
                    if page.is_closed():
                        result = {"state": "uncertain", "confirmation": None, "url": old_url}
                    else:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                        result = browser_form.verify_page(page, gate, old_url, old_body)
                        capture(work, ident, page, "assisted " + result["state"])
                    return record_outcome(work, ident, result, version)
                if gate.review_error:
                    raise NeedsReview(gate.review_error)
                with work.connection() as db, db:
                    update(db, ident, "needs_review", "Assisted session ended without a recorded submission.")
                return {"status": "needs_review"}
            except Exception:
                if not page.is_closed():
                    capture(work, ident, page, "assisted failure")
                if gate.sent:
                    return record_outcome(work, ident, {"state": "uncertain", "confirmation": None, "url": target}, version)
                raise
    except PolicyBlocked as error:
        with work.connection() as db, db:
            update(db, ident, "blocked", str(error))
            update_circuit(db)
        return {"status": "blocked"}
    except browser_form.Assisted as error:
        return assisted_external(work, ident, target, str(error))
    except Exception:
        with work.connection() as db, db:
            update(db, ident, "needs_review", "The assisted session could not complete. Use the employer link if you wish to proceed manually.")
        return {"status": "needs_review"}


def assisted_external(work, ident, target, reason):
    opened = webbrowser.open(target, new=2) if browser_form.origin(target) else False
    text = "Opened the employer page in your browser." if opened else "Open the employer link from the job page."
    with work.connection() as db, db:
        update(db, ident, "needs_review", f"{reason} {text} Record the result manually.")
    return {"status": "needs_review"}


def manual_done(work, ident):
    with work.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        job = get_job(db, ident)
        if job["status"] in {"applied", "interview", "offer", "closed", "rejected", "withdrawn"}:
            raise ValueError("The job already has an application or a closed status.")
        row = db.execute("SELECT status FROM auto_applications WHERE job_id=?", (ident,)).fetchone()
        if not row or row[0] not in {"needs_review", "probable"}:
            raise ValueError("There is no assisted or pending application to confirm.")
        change_status(db, ident, "applied", "Application confirmed manually by the user.")
        db.execute("UPDATE auto_attempt_log SET confirmation_state='manual',checked_at=? WHERE job_id=?",
                   (analytics.timestamp(), ident))
        db.execute("UPDATE auto_applications SET confirmation=?,submitted_at=COALESCE(submitted_at,?) WHERE job_id=?",
                   ("Confirmed by the user.", analytics.timestamp(), ident))
        update(db, ident, "submitted", "User confirmed the application.")
