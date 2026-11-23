"""Outreach personalization, local tracking and reminders."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from smartplier import jobhunt
from smartplier import outreach
from smartplier import tracker
from smartplier.scoring import ProviderError
from tests.test_scoring_cli import PROFILE, gemini_response


POSTING = """Build inference services with Python and FastAPI.
Team: Applied AI
Recruiter: Priya Sharma | priya@example.org
Recruiter: Priya Sharma
Contact: Talent Acquisition Team
"""


class OutreachTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "test.sqlite3"
        with tracker.database(self.db_path) as db:
            self.job_id = tracker.add_job(db, "AI Engineer", "Sample", "Hyderabad", POSTING, "")
            tracker.change_status(db, self.job_id, "to_apply", "Shortlisted.")
            self.job = tracker.get_job(db, self.job_id)
        self.start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jobhunt.main(["--profile", str(Path(__file__).parent / "fixtures" / "profile.example.json"), "--db", str(self.db_path), "outreach", *args])
        return code, out.getvalue(), err.getvalue()

    def test_mocked_llm_uses_only_profile_evidence_and_saves_drafts(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open", return_value=gemini_response({
                "evidence_id": "lantern-api", "invented_claim": "Seven years at another employer",
            }),
        ) as provider:
            code, output, error = self.cli("draft", "1")
            self.assertEqual(code, 0, error)
            self.assertIn("Contact: Priya Sharma", output)
            self.assertIn("Email subject: Interest in the AI Engineer role at Sample", output)
            self.assertIn("I served synthetic sensor forecasts through a small FastAPI service.", output)
            self.assertNotIn("Seven years", output)
            request = json.loads(provider.call_args.args[0].data)
            prompt = request["contents"][0]["parts"][0]["text"]
            for field in ("name", "email", "phone", "github", "linkedin"):
                self.assertNotIn(PROFILE["basics"][field], prompt)
            self.assertNotIn("Priya Sharma", prompt)
            self.assertNotIn("priya@example.org", prompt)
            self.assertIn("lantern-api", prompt)
            self.assertEqual(self.cli("show", "1")[0], 0)
            self.assertEqual(provider.call_count, 1)
        with tracker.database(self.db_path) as db:
            row = outreach.outreach_row(db, self.job_id)
            self.assertIsNone(row["sent_at"])
            self.assertEqual(json.loads(row["drafts"])["evidence_id"], "lantern-api")

    def test_unknown_id_or_provider_error_uses_grounded_local_match(self):
        class MockProvider:
            def generate_json(self, *args):
                return {"evidence_id": "made-up-id"}

        selected = outreach.make_drafts(self.job, PROFILE, MockProvider(), "Asha Rao")
        self.assertIn("unknown evidence ID", selected["warning"])
        self.assertIn("Hi Asha,", selected["email"])
        self.assertIn(selected["evidence_id"], outreach.evidence_options(PROFILE))
        with patch.object(MockProvider, "generate_json", side_effect=ProviderError("test")):
            self.assertIn("Gemini was unavailable", outreach.make_drafts(self.job, PROFILE, MockProvider())["warning"])

    def test_contact_fallback_search_terms_and_note_length(self):
        self.assertEqual(outreach.contact_names(POSTING), ["Priya Sharma"])
        self.assertEqual(outreach.contact_names("Please contact O’Neil James at jane@example.org."),
                         ["O’Neil James"])
        unknown = {**dict(self.job), "description": "Help develop APIs.\nTeam: ML Platform",
                   "title": "Long technical position title " * 12}
        drafts = outreach.make_drafts(unknown, PROFILE)
        self.assertEqual(drafts["contact"], "")
        self.assertTrue(drafts["email"].startswith("Hi,\n"))
        self.assertLessEqual(len(drafts["note"]), outreach.NOTE_LIMIT)
        links = [parse_qs(urlsplit(url).query)["q"][0] for _, url in outreach.search_links(unknown)]
        self.assertIn('"Sample" (recruiter OR "talent acquisition")', links[0])
        self.assertIn('site:linkedin.com/in "Sample" "ML Platform" engineer', links[1])

    def test_silence_reminder_until_reply_follow_up_or_closed(self):
        with tracker.database(self.db_path) as db:
            drafts = outreach.make_drafts(self.job, PROFILE)
            outreach.store_drafts(db, self.job, drafts, now=self.start)
            self.assertFalse(outreach.due_jobs(db, now=self.start + timedelta(days=30)))
            with self.assertRaisesRegex(outreach.OutreachError, "initial message as sent"):
                outreach.follow_up(self.job, outreach.outreach_row(db, self.job_id))
            outreach.record_outreach(db, self.job_id, "sent", "email", now=self.start)
            self.assertFalse(outreach.record_outreach(db, self.job_id, "sent", "linkedin",
                                                      now=self.start + timedelta(days=1)))
            six_days = self.start + timedelta(days=6)
            self.assertFalse(outreach.due_jobs(db, now=six_days - timedelta(seconds=1)))
            due, = outreach.due_jobs(db, now=six_days)
            self.assertEqual((due["id"], due["channel"]), (self.job_id, "email"))
            subject, follow_up = outreach.follow_up(self.job, outreach.outreach_row(db, self.job_id))
            self.assertTrue(subject.startswith("Re: "))
            self.assertIn("follow up on my note about the AI Engineer opening at Sample", follow_up)
            self.assertIn("Hi Priya,", follow_up)
            with self.assertRaisesRegex(outreach.OutreachError, "linked to a sent message"):
                outreach.store_drafts(db, self.job, drafts, now=six_days)
            tracker.change_status(db, self.job_id, "closed", "Posting closed.")
            self.assertFalse(outreach.due_jobs(db, now=six_days))
            tracker.change_status(db, self.job_id, "to_apply", "Reopened.")
            outreach.record_outreach(db, self.job_id, "followed-up", now=six_days)
            self.assertFalse(outreach.due_jobs(db, now=six_days))

            reply_job = tracker.add_job(db, "Python Developer", "Sample", "Remote", POSTING, "")
            outreach.store_drafts(db, tracker.get_job(db, reply_job), drafts, now=self.start)
            outreach.record_outreach(db, reply_job, "sent", "linkedin", now=self.start)
            outreach.record_outreach(db, reply_job, "replied", now=self.start + timedelta(days=2))
            self.assertFalse(outreach.due_jobs(db, now=six_days))

    def test_due_cli_watch_reports_each_new_reminder_once(self):
        with tracker.database(self.db_path) as db:
            outreach.store_drafts(db, self.job, outreach.make_drafts(self.job, PROFILE), now=self.start)
            outreach.record_outreach(db, self.job_id, "sent", "email", now=self.start)
        with patch("smartplier.outreach.clock", return_value=self.start + timedelta(days=7)), patch(
            "smartplier.jobhunt.time.sleep", side_effect=[None, KeyboardInterrupt],
        ) as sleep:
            code, output, error = self.cli("due", "--interval-minutes", "15")
        self.assertEqual(code, 0, error)
        self.assertEqual(output.count("Follow-up due"), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [900, 900])


if __name__ == "__main__":
    unittest.main()
