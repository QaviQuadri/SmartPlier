"""Background execution and persistent synchronization state for the dashboard."""
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from smartplier import analytics
from smartplier import auto_apply
from smartplier import discovery
from smartplier import form_recipes
from smartplier import screening
from smartplier.credentials import gmail_credentials
from smartplier.discovery_mail import gmail_batch
from smartplier.discovery_sources import ALERT_DOMAINS, ats_batch, rss_batch
from smartplier.resume import generate_resume, replace_text
from smartplier.scoring import ProviderError, evaluate
from smartplier.tracker import database, get_job, save_score, scoring_queue

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_sync (
 id INTEGER PRIMARY KEY, run TEXT NOT NULL, source TEXT NOT NULL, trigger TEXT NOT NULL,
 started TEXT NOT NULL, finished TEXT, metrics TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS web_sync_source ON web_sync(source,id);
CREATE TABLE IF NOT EXISTS web_gmail_seen (identity TEXT PRIMARY KEY);
"""

def now():
    return datetime.now(timezone.utc)

def stamp():
    return now().isoformat(timespec="seconds")

def prepare(path, profile=None):
    path = Path(path)
    if path.is_file():
        with closing(sqlite3.connect(path)) as conn:
            migrated = conn.execute("SELECT 1 FROM sqlite_master WHERE name='screening_answers'").fetchone()
            if migrated and profile is not None and Path(profile).is_file():
                migrated = conn.execute("SELECT 1 FROM web_settings WHERE key='screening_bank_imported'").fetchone()
            recipes_ready = conn.execute("SELECT 1 FROM sqlite_master WHERE name='form_recipes'").fetchone()
            attempts_ready = conn.execute("SELECT 1 FROM sqlite_master WHERE name='auto_attempt_log'").fetchone()
            diagnostics_ready = conn.execute("SELECT 1 FROM sqlite_master WHERE name='auto_diagnostics'").fetchone()
            if not migrated or not recipes_ready or not attempts_ready or not diagnostics_ready:
                stage = "screening" if not migrated else "recipes"
                target = path.with_name(path.stem + f"-before-{stage}-" + now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
                with closing(sqlite3.connect(target)) as backup:
                    conn.backup(backup)
    with database(path, profile) as conn:
        conn.executescript(SCHEMA + auto_apply.SCHEMA + screening.SCHEMA + form_recipes.SCHEMA)
        auto_apply.recover(conn)
        with conn:
            if profile is not None and Path(profile).is_file():
                screening.import_legacy(conn, json.loads(Path(profile).read_text(encoding="utf-8")))
            conn.execute("UPDATE web_sync SET finished=?, error='Application stopped before this synchronization completed.' WHERE finished IS NULL", (stamp(),))

class Workspace:
    def __init__(self, db, profile, provider):
        self.db, self.profile, self.provider = Path(db), Path(profile), provider
        self.config = self.profile.parent / "discovery.json"
        if not self.config.exists():
            self.config = Path(__file__).resolve().parents[2] / "examples" / "discovery.example.json"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.active = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Smartplier")
        self.next_run = now() + timedelta(minutes=self.settings()["interval"])
        self.next_receipt_check = now()
        self.timer = threading.Thread(target=self.schedule, daemon=True)
        self.timer.start()

    def connection(self):
        return database(self.db, self.profile)

    def gmail_configured(self):
        try:
            return all(gmail_credentials())
        except (ValueError, OSError):
            return False

    def settings(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM web_settings WHERE key='settings'").fetchone()
        if row:
            return json.loads(row["value"])
        config = json.loads(self.config.read_text(encoding="utf-8-sig"))
        if self.gmail_configured():
            config.setdefault("gmail", {})["enabled"] = True
        companies, feeds, adzuna, gmail, roles = discovery.validate_options(config)
        return {"enabled": True, "interval": 240, "discovery": {
            "companies": companies, "rss": feeds, "adzuna": adzuna, "gmail": gmail, "filter_target_roles": roles,
        }}

    def save_settings(self, data):
        if type(data["enabled"]) is not bool or type(data["interval"]) is not int or not 1 <= data["interval"] <= 10080:
            raise ValueError("Set an interval between 1 and 10080 minutes.")
        raw = data["discovery"]
        clean = {
            "companies": [{k: x[k] for k in ("provider", "board", "company", "enabled") if k in x} for x in raw.get("companies", [])],
            "rss": [{k: x[k] for k in ("name", "url", "enabled") if k in x} for x in raw.get("rss", [])],
            "gmail": {k: v for k, v in raw.get("gmail", {}).items() if k in {"enabled", "mailbox", "days", "limit"}},
            "adzuna": {k: v for k, v in raw.get("adzuna", {}).items() if k in {"enabled", "queries", "pages", "results_per_page"}},
            "filter_target_roles": raw.get("filter_target_roles", True),
        }
        discovery.validate_options(clean)
        if any(type(x.get("enabled", True)) is not bool for x in clean["companies"] + clean["rss"]):
            raise ValueError("Source enabled flags must be true or false.")
        for entries, key in ((clean["companies"], lambda x: (x["provider"], x["board"].lower())), (clean["rss"], lambda x: x["url"])):
            if len({key(x) for x in entries}) != len(entries):
                raise ValueError("Remove duplicate board or feed entries.")
        with self.lock, self.connection() as db, db:
            db.execute("INSERT OR REPLACE INTO web_settings VALUES ('settings',?)",
                       (json.dumps({"enabled": data["enabled"], "interval": data["interval"], "discovery": clean}),))
            self.next_run = now() + timedelta(minutes=data["interval"])

    def profile_data(self):
        return json.loads(self.profile.read_text(encoding="utf-8-sig"))

    def save_preferences(self, prefs):
        allowed = ("target_roles", "deal_breakers", "avoid_companies")
        if any(not isinstance(prefs[k], list) or any(not isinstance(x, str) or not x.strip() for x in prefs[k]) for k in allowed):
            raise ValueError("Enter one nonempty preference per line.")
        if type(prefs["min_score_to_apply"]) is not int or not 0 <= prefs["min_score_to_apply"] <= 100:
            raise ValueError("The shortlist score must be between 0 and 100.")
        if type(prefs["open_to_remote"]) is not bool:
            raise ValueError("Choose whether remote jobs are eligible.")
        with self.lock:
            profile = self.profile_data()
            profile["preferences"].update({k: prefs[k] for k in (*allowed, "min_score_to_apply", "open_to_remote")})
            replace_text(self.profile, json.dumps(profile, ensure_ascii=False, indent=2) + "\n")

    def sources(self, config):
        boards, feeds, adzuna, gmail, _ = discovery.validate_options(deepcopy(config))
        return [
            *[(f"{x['provider']}:{x['board']}", x['company'], x.get("enabled", True), True, "ats", x) for x in boards],
            *[("rss:" + x["url"], x["name"], x.get("enabled", True), True, "rss", x) for x in feeds],
            ("gmail", "Gmail IMAP", gmail.get("enabled", False), self.gmail_configured(), "gmail", gmail),
            ("adzuna", "Adzuna India", adzuna.get("enabled", False), bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY")), "adzuna", adzuna),
        ]

    def snapshot(self):
        with self.lock:
            return deepcopy(list(self.active.values()))

    def queue(self, key, name, items, action):
        with self.lock:
            running = [t for t in self.active.values() if t["status"] in {"queued", "running"}]
            if any(t["key"] == key or key.startswith("score:") and t["key"].startswith("score:") for t in running):
                raise ValueError("That operation is already queued or running.")
            if len(running) >= 20:
                raise ValueError("The local queue is full. Wait for a task to finish.")
            if not items:
                raise ValueError("There is nothing to run.")
            if len(self.active) >= 30:
                keep = {t["id"] for t in running} | set(list(self.active)[-20:])
                self.active = {key: task for key, task in self.active.items() if key in keep}
            ident = uuid4().hex
            task = {"id": ident, "key": key, "name": name, "status": "queued", "total": len(items),
                    "completed": 0, "successes": 0, "errors": [], "results": [], "current": ""}
            self.active[ident] = task
            self.pool.submit(self.execute, task, items, action)
            return ident

    def execute(self, task, items, action):
        with self.lock:
            task["status"] = "running"
        for label, item in items:
            if self.stop.is_set():
                break
            with self.lock:
                task["current"] = label
            try:
                result = action(item, task["id"])
            except (ValueError, ProviderError) as error:
                with self.lock:
                    task["errors"].append(f"{label}: {error}")
            except Exception:
                with self.lock:
                    task["errors"].append(f"{label}: Operation failed. Check the local database and source configuration.")
            else:
                with self.lock:
                    task["successes"] += 1
                    task["results"].append({"label": label, "result": result})
            finally:
                with self.lock:
                    task["completed"] += 1
        with self.lock:
            task["status"] = "cancelled" if task["completed"] < task["total"] else "completed" if not task["errors"] else "partial" if task["successes"] else "failed"
            task["current"] = ""
        if task["key"] == "discovery" and task["successes"] and not self.stop.is_set():
            try:
                auto_apply.queue(self, scheduled=task["name"] == "Scheduled discovery")
            except ValueError:
                pass

    def queue_discovery(self, source=None, trigger="manual"):
        config = self.settings()["discovery"]
        entries = [(x[1], x) for x in self.sources(config) if (x[0] == source if source else x[2])]
        return self.queue("discovery", "Scheduled discovery" if trigger == "scheduled" else "Discover jobs", entries,
                          lambda item, run: self.sync(item, run, trigger, config.get("filter_target_roles", True)))

    def sync(self, item, run, trigger, roles):
        key, label, _, configured, kind, options = item
        with self.connection() as db, db:
            attempt = db.execute("INSERT INTO web_sync(run,source,trigger,started) VALUES (?,?,?,?)",
                                 (run, key, trigger, stamp())).lastrowid
        try:
            if not configured:
                raise ValueError("Credentials are required. See Settings.")
            if kind == "ats":
                batch = ats_batch(options)
            elif kind == "rss":
                batch = rss_batch(options)
            elif kind == "adzuna":
                batch = discovery.run_adzuna(options)
            else:
                with self.connection() as db:
                    seen = {row[0] for row in db.execute("SELECT identity FROM web_gmail_seen")}
                user, password = gmail_credentials()
                batch = gmail_batch(options, user, password, seen)
            with self.connection() as db, db:
                db.execute("BEGIN IMMEDIATE")
                stats = dict(discovery.import_batch(db, batch, self.profile_data(), roles))
                stats.update(fetched=len(batch.postings), checked=batch.checked, processed=len(batch.message_ids))
                db.executemany("INSERT OR IGNORE INTO web_gmail_seen VALUES (?)", [(x,) for x in batch.message_ids])
                db.execute("UPDATE web_sync SET finished=?, metrics=? WHERE id=?", (stamp(), json.dumps(stats), attempt))
            return stats
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else "Synchronization failed while reading or importing source data."
            with self.connection() as db, db:
                db.execute("UPDATE web_sync SET finished=?,error=? WHERE id=?", (stamp(), message, attempt))
            raise ValueError(message) from None

    def history(self, source=None):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM web_sync WHERE (? IS NULL OR source=?) ORDER BY id DESC LIMIT 100", (source, source))
            return [{**dict(row), "metrics": json.loads(row["metrics"]) if row["metrics"] else None} for row in rows]

    def discovery_view(self):
        settings = self.settings()
        history = self.history()
        with self.connection() as db:
            states = {row["source"]: (row["attempt"], row["success"]) for row in db.execute(
                """SELECT source, MAX(started) AS attempt,
                   MAX(CASE WHEN error IS NULL AND metrics IS NOT NULL THEN finished END) AS success
                   FROM web_sync GROUP BY source"""
            )}
        return {"settings": settings, "sources": [
            {"id": key, "name": name, "enabled": enabled, "configured": configured, "type": kind,
             "last_attempt": states.get(key, (None, None))[0], "last_success": states.get(key, (None, None))[1],
             "last_run": next((row for row in history if row["source"] == key), None)}
            for key, name, enabled, configured, kind, _ in self.sources(settings["discovery"])],
            "history": history, "senders": ALERT_DOMAINS, "next": self.next_run.isoformat() if settings["enabled"] else None,
            "scheduled_last": next((row["started"] for row in history if row["trigger"] == "scheduled"), None),
            "scheduler_state": ("disabled" if not settings["enabled"] else
                                "running" if any(t["name"] == "Scheduled discovery" and t["status"] in {"queued", "running"}
                                                 for t in self.snapshot()) else "waiting"),
            "gemini": bool(self.provider)}

    def schedule(self):
        while not self.stop.wait(2):
            with self.lock:
                try:
                    settings = self.settings()
                    if settings["enabled"] and now() >= self.next_run:
                        self.queue_discovery(trigger="scheduled")
                        self.next_run = now() + timedelta(minutes=settings["interval"])
                except (ValueError, OSError, sqlite3.Error):
                    pass
                try:
                    auto_apply.queue_due(self)
                except (ValueError, OSError, sqlite3.Error):
                    pass
                if now() >= self.next_receipt_check:
                    try:
                        self.check_receipts()
                    except (ValueError, OSError, sqlite3.Error):
                        pass
                    self.next_receipt_check = now() + timedelta(minutes=15)

    def check_receipts(self, *, requested=False):
        with self.connection() as db:
            pending = db.execute("SELECT 1 FROM auto_attempt_log WHERE confirmation_state='probable' LIMIT 1").fetchone()
        if not requested and not pending:
            return None
        return self.queue("auto-receipts", "Check application receipts", [("Gmail receipt check", None)],
                          lambda _item, _run: auto_apply.reconcile_receipts(self))

    def submit_score(self, ident=None):
        with self.connection() as db:
            jobs = [get_job(db, ident)] if ident is not None else scoring_queue(db)
        return self.queue(f"score:{ident or 'all'}", "Score jobs", [(f"Job #{row['id']}", row['id']) for row in jobs], self.score)

    def score(self, ident, _run):
        with self.connection() as db:
            job = get_job(db, ident)
            counts = analytics.feedback_counts(db) if self.provider else {}
        result = evaluate(job, self.profile_data(), self.provider, analytics.feedback_for(counts, job["title"]))
        with self.connection() as db:
            save_score(db, job, result)
        return {"score": result.details["fit_score"], "method": result.method, "status": result.status}

    def resume(self, ident, force):
        def generate(_item, _run):
            with self.connection() as db:
                _, warnings, cached = generate_resume(db, self.db, self.profile, ident, self.provider, force)
            return {"warnings": warnings, "cached": cached}
        return self.queue(f"resume:{ident}", "Generate resume", [(f"Job #{ident}", ident)], generate)

    def close(self):
        self.stop.set()
        self.timer.join()
        self.pool.shutdown(wait=True, cancel_futures=True)
