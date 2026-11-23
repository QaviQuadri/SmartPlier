import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from smartplier import auto_apply
from smartplier import screening
from smartplier.form_engine import NeedsReview, fill_fields, inspect_summary
from tests.test_form_engine import control, employer_form
from smartplier.tracker import add_job, database
from smartplier.web_service import prepare


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "jobs.sqlite3"
        self.profile_file = Path(folder.name) / "profile.json"
        self.profile = {"basics": {"email": "private@example.org", "name": "Private Candidate"},
                        "skills": ["UniqueProfileSkill"],
                        "verified_screening_answers": {"Start preference": "Morning"}}
        self.profile_file.write_text(json.dumps(self.profile), encoding="utf-8")
        prepare(self.db, self.profile_file)
        self.provider = Mock()
        self.provider.generate_json.return_value = {"key": "unknown"}
        self.work = SimpleNamespace(connection=lambda: database(self.db), provider=self.provider)

    def job(self, company="Employer"):
        with database(self.db) as db, db:
            ident = add_job(db, "Python Developer", company, "Remote", "Python", "https://example.org/" + company)
            db.execute("""INSERT INTO auto_applications(job_id,identity,status,updated_at,detail)
                       VALUES (?,?,'needs_review','stamp','Waiting')""", (ident, str(ident)))
        return {"id": ident, "location": "Remote"}

    def questions(self):
        with database(self.db) as db:
            return screening.inbox(db)

    def session(self, job, *, plan=True):
        return screening.Session(self.work, job, self.profile, "Lever", plan=plan)

    def test_classifier_uses_only_sanitized_metadata_and_closed_keys(self):
        self.provider.generate_json.return_value = {"key": "arbitrary_value"}
        metadata = {"label": "Question for Private Candidate private@example.org",
                    "type": "text", "options": []}
        self.assertEqual(screening.classify(self.provider, metadata, self.profile), "unknown")
        _, payload, schema = self.provider.generate_json.call_args.args
        self.assertEqual(set(json.loads(payload)), {"label", "type", "options"})
        for private in ("Private Candidate", "private@example.org", "UniqueProfileSkill", "Morning"):
            self.assertNotIn(private, payload + json.dumps(schema))

    def test_unknown_question_collapses_and_saving_unblocks_all_linked_jobs(self):
        first, second = self.job(), self.job("Other")
        fields = [control("custom", "Preferred  shift?"), control("resume", "Resume", "file")]
        form, locators = employer_form()
        summary = inspect_summary(form, fields, self.profile["basics"], self.profile, self.session(first))
        self.assertIn("preferred shift?", summary["unanswered"])
        fields[0]["label"] = "  preferred shift "
        self.assertIsNone(self.session(second).resolve(fields[0], fields, True))
        question, = self.questions()
        self.assertEqual(question["count"], 2)
        self.assertEqual(len(question["employers"]), 2)
        self.assertEqual(self.provider.generate_json.call_count, 1)
        with database(self.db) as db:
            self.assertEqual(screening.save(db, question["id"], {"value": "Morning"}), [first["id"], second["id"]])
        self.assertEqual(self.questions(), [])
        fill_fields(form, fields, self.profile["basics"], self.profile, "evidence.pdf", self.session(first, plan=False))
        locators['[id="custom"]'].fill.assert_called_once_with("Morning", timeout=1500)
        self.assertEqual(self.provider.generate_json.call_count, 1)
        unapproved = screening.Session(self.work, first, self.profile, "Lever", plan=False, approved=set())
        self.assertIsNone(unapproved.resolve(fields[0], fields, True))

    def test_mismatch_to_form_choice_stays_parked_and_requires_exact_option(self):
        job = self.job()
        field = control("shift", "Start preference", "select-one",
                        options=[{"label": "Evening", "value": "e", "disabled": False}])
        self.assertIsNone(self.session(job).resolve(field, [field], True))
        question, = self.questions()
        with database(self.db) as db:
            with self.assertRaisesRegex(ValueError, "available options"):
                screening.save(db, question["id"], {"value": "Morning"})
            self.assertEqual(screening.save(db, question["id"], {"value": " evening "}), [job["id"]])
        self.assertEqual(self.session(job, plan=False).resolve(field, [field], True), "Evening")

    def test_explicit_eeo_policy_and_non_delegable_attestations(self):
        job = self.job()
        self.provider.generate_json.return_value = {"key": "eeo_gender"}
        options = [{"label": label, "value": label, "disabled": False} for label in ("Woman", "Prefer not to say")]
        field = control("eeo", "Gender", "select-one", options=options)
        self.assertIsNone(self.session(job).resolve(field, [field], True))
        question, = self.questions()
        with database(self.db) as db:
            with self.assertRaisesRegex(ValueError, "explicit policy"):
                screening.save(db, question["id"], {"value": "Prefer not to say"})
            with self.assertRaisesRegex(ValueError, "screening policy"):
                screening.save(db, question["id"], {"value": "Woman", "policy": "decline"})
            screening.save(db, question["id"], {"value": "Prefer not to say", "policy": "decline"})
        self.assertEqual(self.session(job, plan=False).resolve(field, [field], True), "Prefer not to say")
        legal = control("signature", "I certify that the information is accurate", "checkbox")
        with self.assertRaises(NeedsReview):
            fill_fields(employer_form()[0], [legal, control("resume", "Resume", "file")],
                        self.profile["basics"], self.profile, "smartplier.resume.pdf", self.session(job))
        question, = self.questions()
        with database(self.db) as db, self.assertRaisesRegex(ValueError, "manual review"):
            screening.save(db, question["id"], {"value": "Yes", "policy": "verified_legal"})
        self.assertEqual(self.provider.generate_json.call_count, 1)

    def test_backup_preserves_old_records_and_import_is_idempotent(self):
        job = self.job()
        with database(self.db) as db, db:
            db.execute("DROP TABLE screening_answers")
            db.execute("DELETE FROM web_settings WHERE key='screening_bank_imported'")
        prepare(self.db, self.profile_file)
        backup, = self.db.parent.glob("jobs-before-screening-*.sqlite3")
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT id FROM jobs").fetchone()[0], job["id"])
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='screening_answers'").fetchone())
        with database(self.db) as db, db:
            screening.write_answer(db, "legacy:start preference", "Evening")
        prepare(self.db, self.profile_file)
        with database(self.db) as db:
            self.assertEqual(screening.answer_row(db, "legacy:start preference")[0], "Evening")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertFalse(auto_apply.settings(db)["screening_drafts"])
        self.assertEqual(len(list(self.db.parent.glob("jobs-before-screening-*.sqlite3"))), 1)

    def test_optional_draft_accepts_only_profile_evidence_ids(self):
        self.profile["stories"] = [{"id": "sample", "text": "Built a Python dashboard."}]
        question = {"label": "Why this role?", "type": "textarea", "protection": "", "options": []}
        self.provider.generate_json.return_value = {"ids": ["sample"]}
        self.assertEqual(screening.draft_answer(self.provider, question, self.profile), "Built a Python dashboard.")
        self.provider.generate_json.return_value = {"ids": ["new employer"]}
        with self.assertRaisesRegex(ValueError, "valid evidence"):
            screening.draft_answer(self.provider, question, self.profile)
