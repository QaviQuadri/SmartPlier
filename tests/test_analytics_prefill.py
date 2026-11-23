"""Application analytics, feedback and review-only prefill coverage."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from smartplier import analytics
from smartplier import discovery
from smartplier import jobhunt
from smartplier import prefill
from smartplier import tracker
from smartplier.discovery_sources import SourceBatch, make_posting
from tests.test_scoring_cli import MOCK_RESULT, PROFILE, gemini_response


LEVER = "https://jobs.lever.co/sample/781ee264-00f1-46db-a523-8f31ee4d6541"


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "smartplier.analytics.sqlite3"

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jobhunt.main(["--profile", str(Path(__file__).parent / "fixtures" / "profile.example.json"), "--db", str(self.db), *args])
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def test_snapshot_and_report_keep_application_dimensions_and_reply(self):
        with tracker.database(self.db) as db:
            announcement = make_posting("ML Engineer", "Example", "Hyderabad", "Python", LEVER, "job-1")
            discovery.import_batch(db, SourceBatch("lever:sample", "Example (lever)", [announcement]), PROFILE)
            with db:
                db.execute("""UPDATE jobs SET score=82, score_method='gemini',
                              scored_at='2020-01-01T00:00:00.000Z' WHERE id=1""")
        self.cli("status", "1", "applied", "--variant", "Tailored")
        self.cli("status", "1", "interview")
        self.assertIn("already recorded", self.cli("analytics", "--reply", "1"))
        self.cli("analytics", "--variant", "1", "Backend v2")
        with tracker.database(self.db) as db:
            tracker.update_location(db, 1, "Dubai, UAE")
            tracker.add_job(db, "Data Analyst", "Other", "Hyderabad", "SQL", "")
            tracker.change_status(db, 2, "rejected", "Skipped the application.")
            tracker.add_job(db, "Python Developer", "Third", "India", "Python", "")
            tracker.change_status(db, 3, "applied", "Submitted manually.", "Standard")
        output = self.cli("analytics")
        for expected in ("1/2 (50.0%)", "AI / ML | 1/1", "Lever | 1/1", "80–100 | 1/1",
                         "Backend v2 | 1/1", "Python / Backend | 0/1", "Standard | 0/1"):
            self.assertIn(expected, output)
        self.assertNotIn("Data |", output)

    def test_migrates_old_status_history_without_guessing_unknown_variant_or_score(self):
        with tracker.database(self.db) as db:
            job = tracker.add_job(db, "Python Developer", "Example", "Hyderabad", "Python", "")
            with db:
                db.execute("PRAGMA user_version = 1")
                tracker.record_status(db, job, "unscored", "applied", "Submitted.")
                tracker.record_status(db, job, "applied", "rejected", "Reply from company.")
                db.execute("""UPDATE jobs SET score_method='gemini', score=88,
                              scored_at='2099-01-01T00:00:00+00:00' WHERE id=?""", (job,))
        with tracker.database(self.db) as db:
            record = analytics.application(db, job)
            self.assertIsNone(record["score"])
            self.assertEqual(record["variant"], "Unrecorded")
            self.assertIsNotNone(record["response_at"])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIn("1/1 (100.0%)", self.cli("analytics"))

    def test_mocked_gemini_receives_mature_role_counts_and_keeps_its_fit_score(self):
        now = datetime.now(timezone.utc)
        with tracker.database(self.db) as db, db:
            for index in range(6):
                job = tracker.add_job(db, "ML Engineer", f"Private Company {index}", "India", "Python", "")
                old = index < 5
                analytics.record_application(
                    db, tracker.get_job(db, job), analytics.timestamp(now - timedelta(days=18 if old else 3)),
                    "Private resume", response_at=analytics.timestamp(now) if index in (0, 1, 5) else None,
                )
            scored = tracker.add_job(db, "ML Engineer", "Sample", "Hyderabad", "Python required.", "")
            insufficient = tracker.add_job(db, "Data Analyst", "Sample", "India", "SQL required.", "")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open",
            side_effect=[gemini_response(MOCK_RESULT), gemini_response(MOCK_RESULT)],
        ) as request:
            self.assertIn(f"#{scored} to_apply 83/100 [gemini]", self.cli("score", "--id", str(scored)))
            self.assertIn(f"#{insufficient} to_apply 83/100 [gemini]", self.cli("score", "--id", str(insufficient)))
        sent = [json.loads(call.args[0].data) for call in request.call_args_list]
        payloads = [json.loads(value["contents"][0]["parts"][0]["text"]) for value in sent]
        self.assertEqual(payloads[0]["anonymous_application_history"], {
            "role_type": "AI / ML", "applications": 5, "responses": 2, "minimum_age_days": 14,
        })
        self.assertNotIn("anonymous_application_history", payloads[1])
        serialized = json.dumps(sent)
        for text in ("Private Company", "Private resume", PROFILE["basics"]["email"], PROFILE["basics"]["name"]):
            self.assertNotIn(text, serialized)


class PrefillTests(unittest.TestCase):
    def test_supported_lever_links_and_read_only_network_guard(self):
        self.assertEqual(prefill.lever_apply_url(LEVER + "?source=example"), LEVER + "/apply")
        self.assertEqual(prefill.lever_apply_url(LEVER.replace("jobs.", "jobs.eu.") + "/apply/"),
                         LEVER.replace("jobs.", "jobs.eu.") + "/apply")
        for url in ("https://www.linkedin.com/jobs/view/123", LEVER.replace("https:", "http:"),
                    LEVER.replace("jobs.lever.co", "jobs.lever.co.example.org"), LEVER + "/legal",
                    LEVER.replace("jobs.lever.co", "user@jobs.lever.co")):
            with self.subTest(url=url), self.assertRaises(prefill.PrefillError):
                prefill.lever_apply_url(url)
        route = MagicMock()
        route.request.url, route.request.method = LEVER + "/apply", "POST"
        prefill.route_request(route, "jobs.lever.co")
        route.abort.assert_called_once()
        route.fetch.assert_not_called()
        route.reset_mock()
        route.request.method = "GET"
        route.fetch.return_value.status = 302
        prefill.route_request(route, "jobs.lever.co")
        route.fetch.assert_called_once_with(max_redirects=0)
        route.abort.assert_called_once()
        route.fulfill.assert_not_called()

    def test_contact_plan_leaves_nonstandard_or_prefilled_fields_untouched(self):
        page, form = MagicMock(), MagicMock()
        page.locator.return_value.filter.return_value.filter.return_value = form
        form.count.return_value = 1
        fields = {key: MagicMock() for key, _, _ in prefill.FIELDS}
        form.locator.side_effect = {selector: fields[key] for key, _, selector in prefill.FIELDS}.__getitem__
        for field in fields.values():
            field.count.return_value = 1
            field.is_visible.return_value = True
            field.is_editable.return_value = True
            field.input_value.return_value = ""
        fields["email"].input_value.return_value = "already@example.org"
        fields["phone"].is_visible.return_value = False
        filled = prefill.contact_fields(page, PROFILE["basics"])
        self.assertIn("Name", filled)
        self.assertNotIn("Email", filled)
        self.assertNotIn("Phone", filled)
        fields["email"].fill.assert_not_called()
        fields["phone"].fill.assert_not_called()
        fields["linkedin"].fill.assert_called_once_with("https://" + PROFILE["basics"]["linkedin"])


if __name__ == "__main__":
    unittest.main()
