from contextlib import closing
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
import json
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from smartplier import analytics
from smartplier import auto_apply
from smartplier import confirmation_mail
from smartplier.form_engine import NeedsReview, PolicyBlocked
from tests.test_scoring_cli import PROFILE
from smartplier.tracker import add_job, database, get_job
from smartplier.web_service import prepare


class ReceiptTests(unittest.TestCase):
    def test_receipt_checks_sender_company_role_time_and_posting(self):
        now = analytics.date(analytics.timestamp())
        row = {"reserved_at": analytics.timestamp(now), "company": "Example Robotics", "title": "Python Developer",
               "url": "https://jobs.lever.co/example/781ee264-00f1-46db-a523-8f31ee4d6541/apply"}
        msg = EmailMessage(policy=policy.default)
        msg["From"] = "Example Robotics <noreply@lever.co>"
        msg["To"] = "candidate@example.test"
        msg["Date"] = format_datetime(now)
        msg["Subject"] = "Application received: Python Developer"
        msg["Authentication-Results"] = "mx.google.com; dmarc=pass header.from=lever.co;"
        msg.set_content("Thank you for applying at Example Robotics: " + row["url"])
        self.assertTrue(confirmation_mail.matches_receipt(msg, row, "candidate@example.test"))
        self.assertFalse(confirmation_mail.matches_receipt(msg, dict(row, title="ML Engineer"), "candidate@example.test"))
        self.assertFalse(confirmation_mail.matches_receipt(msg, dict(row, url=row["url"].replace("781e", "781f")),
                                                           "candidate@example.test"))
        self.assertFalse(confirmation_mail.matches_receipt(msg, dict(row, reserved_at=analytics.timestamp(now + timedelta(hours=1))),
                                                           "candidate@example.test"))
        msg.replace_header("Authentication-Results", "mx.google.com; dmarc=pass header.from=other.test;")
        self.assertFalse(confirmation_mail.matches_receipt(msg, row, "candidate@example.test"))


class DurableAutomationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "jobs.sqlite3"
        self.profile = self.path.with_name("profile.json")
        self.profile.write_text(json.dumps(PROFILE), encoding="utf8")
        prepare(self.path, self.profile)
        self.work = SimpleNamespace(connection=lambda: database(self.path), profile_data=lambda: PROFILE)

    def job(self, company="Example Robotics"):
        with self.work.connection() as db, db:
            ident = add_job(db, "Python Developer", company, "Remote", "Python", "https://example.test/" + company.replace(" ", "-"))
            db.execute("UPDATE jobs SET status='to_apply',score=95,score_method='gemini' WHERE id=?", (ident,))
            at = analytics.timestamp()
            db.execute("INSERT INTO job_discovery(job_id,source,first_seen,last_seen,published_at) VALUES (?,?,?,?,?)",
                       (ident, "Fixture", at, at, at))
            auto_apply.single_row(db, get_job(db, ident))
        return ident

    def attempt(self, ident, state="pending", when=None):
        with self.work.connection() as db, db:
            db.execute("UPDATE auto_applications SET attempted_day=?,resume_version='version' WHERE job_id=?",
                       (date.today().isoformat(), ident))
            db.execute("INSERT INTO auto_attempt_log(job_id,reserved_at,snapshot,confirmation_state) VALUES(?,?,'{}',?)",
                       (ident, when or analytics.timestamp(), state))

    def test_probable_holds_then_email_marks_applied(self):
        ident = self.job()
        self.attempt(ident)
        self.assertEqual(auto_apply.record_outcome(self.work, ident,
            {"state": "probable", "confirmation": "Thank you for applying", "url": "https://example.test/thanks"}, "version"),
                         {"status": "probable"})
        with self.work.connection() as db:
            self.assertEqual(get_job(db, ident)["status"], "to_apply")
            with self.assertRaises(PolicyBlocked):
                auto_apply.single_row(db, get_job(db, ident))
        with patch("smartplier.auto_apply.gmail_credentials", return_value=("test@example.test", "password")), patch(
            "smartplier.confirmation_mail.find_receipts", return_value={ident: "receipt-hash"}
        ):
            self.assertEqual(auto_apply.reconcile_receipts(self.work), {"confirmed": 1, "review": 0})
        with self.work.connection() as db:
            self.assertEqual(get_job(db, ident)["status"], "applied")
            self.assertEqual(db.execute("SELECT email_reference FROM auto_attempt_log").fetchone()[0], "receipt-hash")

    def test_uncertain_and_daily_cap_never_retry(self):
        first, second = self.job(), self.job("Another employer")
        with self.work.connection() as db:
            auto_apply.save_settings(db, {"enabled": True, "daily_cap": 1})
        self.attempt(first, "uncertain")
        with self.assertRaisesRegex(NeedsReview, "daily submission cap"):
            auto_apply.reserve(self.work, second, "https://example.test", "version")
        with self.work.connection() as db, self.assertRaises(PolicyBlocked):
            auto_apply.single_row(db, get_job(db, first))

    def test_consent_is_recorded_at_reservation(self):
        ident = self.job()
        snapshot = {"action": "https://example.test/post", "fields": [["privacy", "on"]], "resume_version": "evidence",
                    "consents": ["Privacy policy"]}
        with self.work.connection() as db:
            auto_apply.save_settings(db, {"enabled": True, "privacy_consent": True})
            answers = auto_apply.screening.revision(db)
            signature = auto_apply.input_version(get_job(db, ident), PROFILE, auto_apply.settings(db), answers)
            with db:
                db.execute("INSERT INTO auto_preflight VALUES (?,?)", (ident, json.dumps({"input": signature, "snapshot": snapshot})))
        auto_apply.reserve(self.work, ident, snapshot["action"], "version", PROFILE, answers, snapshot)
        with self.work.connection() as db:
            self.assertEqual(json.loads(db.execute("SELECT snapshot FROM auto_attempt_log").fetchone()[0]), snapshot)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_events WHERE status='consent'").fetchone()[0], 1)

    def test_circuit_pauses_on_two_outcomes_and_ramp_increases_explicitly(self):
        first, second = self.job(), self.job("Another employer")
        self.attempt(first, "uncertain")
        self.attempt(second, "pending")
        auto_apply.record_outcome(self.work, second, {"state": "uncertain", "confirmation": None,
                                                       "url": "https://example.test"}, "version")
        with self.work.connection() as db:
            self.assertIn("Two consecutive", auto_apply.circuit(db)["reason"])
            self.assertEqual(auto_apply.ramp_daily_cap(db), 4)
            auto_apply.resume_autopilot(db)
            self.assertFalse(auto_apply.circuit(db)["paused"])

    def test_policy_block_spike_and_default_autopilot_authorization(self):
        jobs = [self.job(f"Employer {n}") for n in range(3)]
        with self.work.connection() as db:
            self.assertFalse(auto_apply.settings(db)["autopilot"])
            with self.assertRaisesRegex(ValueError, "standing authorization"):
                auto_apply.save_settings(db, {"enabled": True, "autopilot": True})
            auto_apply.save_settings(db, {"enabled": True, "autopilot": True, "authorize_autopilot": True})
            with db:
                for ident in jobs:
                    auto_apply.update(db, ident, "blocked", "Fixture blocked")
                auto_apply.update_circuit(db)
            self.assertIn("Three distinct", auto_apply.circuit(db)["reason"])
            with self.assertRaisesRegex(NeedsReview, "authorization"):
                auto_apply.require_autopilot(db, {"autopilot_due": analytics.timestamp()})

    def test_automation_migration_backups_are_idempotent(self):
        ident = self.job()
        with self.work.connection() as db, db:
            db.execute("DROP TABLE form_recipes")
            db.execute("DROP TABLE auto_attempt_log")
        prepare(self.path, self.profile)
        prepare(self.path, self.profile)
        backup, = self.path.parent.glob("jobs-before-recipes-*.sqlite3")
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT id FROM jobs").fetchone()[0], ident)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='form_recipes'").fetchone())
        with self.work.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM form_recipes").fetchone()[0], 0)
            self.assertEqual(get_job(db, ident)["status"], "to_apply")

    def test_assisted_access_fallback_is_manual(self):
        ident = self.job()
        with patch("smartplier.auto_apply.webbrowser.open", return_value=True) as open_browser:
            auto_apply.assisted_external(self.work, ident, "https://example.test/apply", "HTTP 403")
        open_browser.assert_called_once_with("https://example.test/apply", new=2)
        with self.work.connection() as db:
            row = db.execute("SELECT status,attempted_day,detail FROM auto_applications WHERE job_id=?", (ident,)).fetchone()
            self.assertEqual((row["status"], row["attempted_day"]), ("needs_review", None))
            self.assertIn("Record the result manually", row["detail"])

    def test_scheduler_observes_review_window_and_cancellation(self):
        ident = self.job()
        self.work.lock = threading.RLock()
        self.work.queue = Mock()
        now = analytics.date(analytics.timestamp())
        with self.work.connection() as db:
            auto_apply.save_settings(db, {"enabled": True, "autopilot": True, "authorize_autopilot": True})
            signature = auto_apply.input_version(get_job(db, ident), PROFILE, auto_apply.settings(db),
                                                 auto_apply.screening.revision(db))
            summary = {"ready": True, "at": analytics.timestamp(now), "input": signature,
                       "autopilot_due": analytics.timestamp(now + timedelta(minutes=3))}
            with db:
                auto_apply.update(db, ident, "ready", "Fixture ready")
                db.execute("INSERT INTO auto_preflight VALUES (?,?)", (ident, json.dumps(summary)))
        auto_apply.queue_due(self.work)
        self.work.queue.assert_not_called()
        summary["autopilot_due"] = analytics.timestamp(now - timedelta(seconds=1))
        with self.work.connection() as db, db:
            db.execute("UPDATE auto_preflight SET summary=? WHERE job_id=?", (json.dumps(summary), ident))
        auto_apply.queue_due(self.work)
        self.assertEqual(self.work.queue.call_args.args[2], [(f"Job #{ident}", ident)])
        self.work.queue.reset_mock()
        auto_apply.cancel_scheduled(self.work, ident)
        auto_apply.queue_due(self.work)
        self.work.queue.assert_not_called()
