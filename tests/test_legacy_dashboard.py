"""Loopback HTTP integration tests for the dashboard."""

import http.client
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode

from smartplier import outreach

from smartplier.dashboard import DashboardServer, FORM_TYPE
from smartplier.discovery import import_batch
from smartplier.discovery_sources import SourceBatch, make_posting
from smartplier.resume import resume_paths
from smartplier.scoring import ENDPOINT, GeminiProvider
from tests.test_scoring_cli import MOCK_RESULT, PROFILE, gemini_response
from smartplier.tracker import database


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        profile = root / "profile.json"
        profile.write_bytes((Path(__file__).parent / "fixtures" / "profile.example.json").read_bytes())
        self.db_path = root / "jobs.sqlite3"
        self.server = DashboardServer(0, self.db_path, profile)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        _, _, html = self.request("GET", "/jobs/new")
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', html)[1]

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection(self.server.host, timeout=3)
        try:
            client.request(method, path, body, headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            client.close()

    def post(self, path, fields=None, **overrides):
        headers = {"Origin": self.server.origin, "Content-Type": FORM_TYPE, **overrides}
        body = urlencode({"csrf": self.csrf, **(fields or {})})
        return self.request("POST", path, body, headers)

    def add(self, **overrides):
        fields = {"title": "Python Developer", "company": "Sample", "location": "",
                  "description": "Python, FastAPI, Redis and Docker. Contact team@example.com.",
                  **overrides}
        status, headers, _ = self.post("/jobs/new", fields)
        self.assertEqual(status, 303)
        return headers["Location"]

    def test_job_workflow_tabs_search_and_history(self):
        job = self.add()
        self.assertEqual(job, "/jobs/1")
        _, _, html = self.request("GET", "/?tab=unscored")
        self.assertIn('Unscored<span>1</span>', html)
        self.assertIn('Python Developer', html)
        self.assertEqual(self.post(job + "/score")[0], 303)
        _, _, html = self.request("GET", "/?tab=review")
        self.assertIn('Needs review<span>1</span>', html)
        self.assertIn("Filter", html)
        self.server.provider = GeminiProvider("mock-key")
        with patch("urllib.request.OpenerDirector.open", return_value=gemini_response(MOCK_RESULT)):
            self.assertEqual(self.post(job + "/location", {"location": "Hyderabad, India"})[0], 303)
        _, _, html = self.request("GET", job)
        for expected in ("Gemini", "<strong>83</strong>", "team@example.com", "Explicit location updated.",
                         "Location is missing.", "Status history"):
            self.assertIn(expected, html)
        self.assertEqual(self.post(job + "/status", {"status": "applied", "reason": "Sent manually."})[0], 303)
        self.add(title="Data Analyst", company="Second", location="Bengaluru")
        _, _, html = self.request("GET", "/?tab=in-progress&q=FASTapi")
        self.assertIn("Python Developer", html)
        self.assertNotIn("Data Analyst", html)
        self.assertIn('In progress<span>1</span>', html)
        self.assertIn('All<span>2</span>', html)
        self.assertIn("No jobs match", self.request("GET", "/?tab=all&q=%25")[2])
        self.assertIn("Sent manually.", self.request("GET", job)[2])

    def test_gemini_only_score_details(self):
        job = self.add(location="Bengaluru, India")
        status, _, html = self.post(job + "/score")
        self.assertEqual(status, 503)
        self.assertIn("Set GEMINI_API_KEY", html)
        self.server.provider = GeminiProvider("mock-key")
        self.assertIn("Score with Gemini", self.request("GET", job)[2])
        with patch("urllib.request.OpenerDirector.open", return_value=gemini_response(MOCK_RESULT)) as provider:
            self.assertEqual(self.post(job + "/score")[0], 303)
            self.assertEqual(provider.call_count, 1)
        _, _, html = self.request("GET", job)
        for expected in ('<strong>83</strong>', 'badge gemini', 'PostgreSQL', 'FastAPI', 'unknown'):
            self.assertIn(expected, html)
        self.assertNotIn("Location deduction", html)
        with database(self.db_path) as db:
            row = db.execute("SELECT score, score_method, status FROM jobs").fetchone()
            self.assertEqual(tuple(row), (83, "gemini", "to_apply"))

    def test_failed_location_rescore_clears_previous_assessment(self):
        job = self.add(location="Hyderabad")
        self.server.provider = GeminiProvider("mock-key")
        with patch("urllib.request.OpenerDirector.open", return_value=gemini_response(MOCK_RESULT)):
            self.assertEqual(self.post(job + "/score")[0], 303)
        failure = HTTPError(ENDPOINT, 401, "sensitive response", {}, None)
        with patch("urllib.request.OpenerDirector.open", side_effect=failure):
            status, _, html = self.post(job + "/location", {"location": "Dubai, UAE"})
        self.assertEqual(status, 502)
        for expected in ("The location was saved.", "HTTP 401", "This job has no score yet.", "Dubai, UAE"):
            self.assertIn(expected, html)
        self.assertNotIn("sensitive response", html)
        with database(self.db_path) as db:
            row = db.execute("SELECT location, status, score_method, score FROM jobs").fetchone()
            self.assertEqual(tuple(row), ("Dubai, UAE", "unscored", None, None))

    def test_request_guards_reject_mutation(self):
        fields = {"title": "Python Developer", "company": "Example", "description": "Python"}
        self.assertEqual(self.request("GET", "/", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.post("/jobs/new", fields, Host="example.com")[0], 403)
        self.assertEqual(self.post("/jobs/new", fields, Origin="https://example.com")[0], 403)
        self.assertEqual(self.post("/jobs/new", fields, Origin="null")[0], 403)
        self.assertEqual(self.request("POST", "/jobs/new", "", {"Content-Type": FORM_TYPE})[0], 403)
        self.assertEqual(self.post("/jobs/new", fields, **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("/jobs/new", {**fields, "csrf": "wrong"})[0], 403)
        self.assertEqual(self.request("GET", "/jobs/1/score")[0], 405)
        self.assertIn("No jobs yet.", self.request("GET", "/")[2])

    def test_second_dashboard_cannot_take_the_same_port(self):
        with self.assertRaises(OSError):
            DashboardServer(self.server.server_port, self.db_path, self.server.profile_path)

    def test_untrusted_text_and_links_are_escaped(self):
        job = self.add(title='<img src=x onerror="alert(1)">', company="& Co",
                       description="</textarea><script>alert(1)</script> linkedin.com/in/test",
                       url="javascript:alert(1)")
        _, headers, html = self.request("GET", job)
        self.assertNotIn("<script>", html)
        self.assertEqual(re.findall(r"<img\b[^>]*>", html),
                         ['<img src="/assets/smartplier-logo.png" alt="Smartplier" width="1536" height="1024">'])
        self.assertNotIn('href="javascript:', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp; Co", html)
        self.assertIn('href="https://www.linkedin.com/in/test"', html)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        _, _, html = self.request("GET", "/?tab=all&q=%3Cimg")
        self.assertIn("&lt;img", html)

    def test_generated_resume_view_uses_only_job_html(self):
        job = self.add()
        self.assertEqual(self.request("GET", job + "/resume")[0], 404)
        path, _ = resume_paths(self.db_path, self.server.profile_path, 1)
        path.parent.mkdir(parents=True)
        path.write_text("<h1>Resume preview</h1>", encoding="utf-8")
        self.assertIn("Open generated resume", self.request("GET", job)[2])
        status, headers, html = self.request("GET", job + "/resume")
        self.assertEqual((status, html), (200, "<h1>Resume preview</h1>"))
        self.assertTrue(headers["Content-Security-Policy"].startswith("sandbox;"))
        self.assertEqual(self.request("GET", "/jobs/2/resume")[0], 404)
        self.assertEqual(self.request("GET", "/jobs/../profile.json")[0], 404)

    def test_discovery_source_and_age_are_visible(self):
        post = make_posting("Python Developer", "Sample", "Hyderabad", "Build APIs.",
                            "https://jobs.example.org/123", "123", "2020-01-01")
        with database(self.db_path) as db:
            import_batch(db, SourceBatch("sample", "A & B", [post]), PROFILE)
        status, _, html = self.request("GET", "/jobs/1")
        self.assertEqual(status, 200)
        self.assertIn("A &amp; B</a>", html)
        self.assertIn("Published 2020-01-01", html)
        self.assertIn("Posting signals", html)
        self.assertIn("days old.", html)

    def test_analytics_dashboard_tracks_application_variant_and_explicit_reply(self):
        job = self.add(title="ML Engineer")
        self.assertIn("No recorded applications yet.", self.request("GET", "/analytics")[2])
        self.assertEqual(self.post(job + "/application", {"action": "reply"})[0], 400)
        self.assertEqual(self.post(job + "/status", {"status": "applied", "variant": "Tailored"} )[0], 303)
        self.assertEqual(self.post(job + "/application", {"action": "variant", "variant": "Custom <Variant>"})[0], 303)
        self.assertEqual(self.post(job + "/application", {"action": "reply"}, Origin="https://example.com")[0], 403)
        self.assertEqual(self.post(job + "/application", {"action": "reply"})[0], 303)
        status, _, html = self.request("GET", "/analytics")
        self.assertEqual(status, 200)
        for expected in ("AI / ML", "Manual", "Unscored", "Custom &lt;Variant&gt;", "100.0%"):
            self.assertIn(expected, html)
        self.assertNotIn("Custom <Variant>", html)

    def test_outreach_dashboard_flow_and_follow_up_reminder(self):
        job = self.add(description="Python and FastAPI.\nRecruiter: Priya Sharma | priya@example.org")
        self.assertIn("Outreach and follow-up", self.request("GET", job)[2])
        path = job + "/outreach"
        self.assertIn("Named in posting · Priya Sharma", self.request("GET", path)[2])
        self.assertEqual(self.post(path, {"action": "sent", "channel": "email"})[0], 400)
        self.assertEqual(self.post(path, {"action": "draft", "contact": "Asha <sample>"})[0], 303)
        _, _, html = self.request("GET", path)
        self.assertIn("Hi Asha,", html)
        self.assertIn("Asha &lt;sample&gt;", html)
        self.assertNotIn("Asha <sample>", html)
        old = datetime.now(timezone.utc) - timedelta(days=7)
        with patch("smartplier.outreach.clock", return_value=old):
            self.assertEqual(self.post(path, {"action": "sent", "channel": "email"})[0], 303)
        self.assertIn("1 follow-up due", self.request("GET", "/")[2])
        self.assertIn("Python Developer", self.request("GET", "/outreach/due")[2])
        self.assertIn("Follow-up due", self.request("GET", path)[2])
        self.assertEqual(self.post(path, {"action": "replied"})[0], 303)
        self.assertIn("No follow-ups due.", self.request("GET", "/outreach/due")[2])

    def test_connections_close_on_success_and_error(self):
        checks = []

        @contextmanager
        def tracked(path, profile_path):
            try:
                with database(path, profile_path) as db:
                    yield db
            finally:
                try:
                    db.execute("SELECT 1")
                except sqlite3.ProgrammingError as error:
                    checks.append(str(error))
                else:
                    checks.append("Connection still open")

        with patch("smartplier.dashboard.database", tracked):
            self.request("GET", "/")
            self.request("GET", "/jobs/5")
            job = self.add()
            self.post(job + "/score")
            self.request("GET", job)
        self.assertEqual(len(checks), 6)
        self.assertTrue(all("closed" in check for check in checks), checks)


if __name__ == "__main__":
    unittest.main()
