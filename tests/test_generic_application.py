import os
import json
import threading
import unittest
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from smartplier import auto_apply
from smartplier import analytics
from smartplier import generic_application as browser_form
from smartplier.form_engine import fill_fields
from tests.test_scoring_cli import PROFILE
from smartplier.tracker import add_job, database, get_job
from smartplier.web_service import prepare

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    sync_playwright = None


class Employer(BaseHTTPRequestHandler):
    posts = []
    gets = []

    def log_message(self, *_args):
        pass

    def reply(self, code, body, **headers):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path.strip("/")
        self.gets.append(path)
        if path == "cross":
            self.reply(302, "", Location=f"http://localhost:{self.server.server_port}/unapproved")
            return
        if path == "thanks":
            self.reply(200, "Thank you for applying")
            return
        if path == "bad":
            self.reply(403, "Access denied")
            return
        if path == "captcha":
            self.reply(200, "<div data-sitekey='test'>Verify you are human</div>")
            return
        if path in {"greenhouse", "ashby", "workable", "custom"}:
            self.reply(200, f"<h1>{path} sample</h1><a href='/{path}/steps'>Apply now</a>")
            return
        if path.endswith("/steps"):
            brand = path.split("/")[0]
            self.reply(200, f'<form action="/{brand}/final" method="get"><button>Next</button></form>')
            return
        extra = {
            "privacy": '<label><input type="checkbox" name="privacy" required>I accept the privacy policy</label>',
            "attestation": '<label><input type="checkbox" required>I certify that my experience is accurate</label>',
        }.get(path, "")
        action = {"replay": "/submit/redirect", "post303": "/submit/receipt"}.get(path, "/submit/normal")
        if path == "offsite":
            action = f"http://localhost:{self.server.server_port}/submit/normal"
        self.reply(200, f"""
            <h1>{path} fixture</h1>
            <form action="{action}" method="post" enctype="multipart/form-data">
              <label>Name <input name="name" required></label>
              <label>Email <input name="recipient" type="email" required></label>
              <label>Résumé <input name="resume" type="file" accept=".pdf" required></label>
              <input type="hidden" name="csrf" value="rotating-test-token">
              {extra}
              <button>Send application</button>
            </form>
        """)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.posts.append((self.path, raw))
        if self.path.endswith("/redirect"):
            self.reply(307, "Moved", Location="/submit/second")
        elif self.path.endswith("/receipt"):
            self.reply(303, "See other", Location="/thanks")
        else:
            self.reply(200, "<h1>Thank you for applying</h1><p>Your application has been submitted.</p>")


@unittest.skipUnless(sync_playwright, "Optional Python Playwright runtime is unavailable")
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Employer)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.root = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.driver = sync_playwright().start()
        cls.addClassCleanup(cls.driver.stop)
        try:
            channel = os.getenv("SMARTPLIER_BROWSER_CHANNEL")
            cls.browser = cls.driver.chromium.launch(**({"channel": channel} if channel else {}))
        except PlaywrightError:
            raise unittest.SkipTest("Install Playwright Chromium or configure SMARTPLIER_BROWSER_CHANNEL.") from None
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        Employer.posts.clear()
        Employer.gets.clear()
        self.context = self.browser.new_context(java_script_enabled=False, service_workers="block")
        self.addCleanup(self.context.close)
        self.gate = browser_form.Gate(self.root, fixture=True)
        self.gate.install(self.context)
        self.page = self.context.new_page()
        self.basics = {"name": "Fixture Candidate", "email": "candidate@example.test"}

    def fill(self, form, details):
        fill_fields(form, details["fields"], self.basics, {}, auto_apply.attachment(b"%PDF-1.4 test file"))
        return browser_form.submission_snapshot(form, details, "resume-fixture")

    def test_finds_lever_greenhouse_ashby_workable_and_custom_layouts(self):
        for name in ("lever", "greenhouse", "ashby", "workable", "custom"):
            with self.subTest(brand=name):
                form, details, nav = browser_form.find_form(self.page, self.gate, self.root + "/" + name)
                self.assertEqual(nav, "direct" if name == "lever" else "apply/next")
                self.assertEqual([f["type"] for f in details["fields"]], ["text", "email", "file", "hidden"])
                self.assertEqual(form.count(), 1)
        self.assertEqual(Employer.posts, [])

    def test_reservation_snapshot_upload_and_probable_confirmation(self):
        form, details, _ = browser_form.find_form(self.page, self.gate, self.root + "/lever")
        snapshot = self.fill(form, details)
        self.assertIn(["recipient", "candidate@example.test"], snapshot["fields"])
        self.assertIn(["csrf", {"redacted": True}], snapshot["fields"])
        self.assertEqual(Employer.posts, [])

        def reserve(url, sent):
            self.assertEqual(Employer.posts, [])
            self.assertEqual(sent, snapshot)
            self.assertEqual(url, self.root + "/submit/normal")

        outcome = browser_form.submit_form(self.page, self.gate, form, details, snapshot, reserve)
        self.assertEqual(outcome["state"], "probable")
        self.assertIn("Thank you for applying", outcome["confirmation"])
        self.assertEqual(len(Employer.posts), 1)
        self.assertIn(b"Resume.pdf", Employer.posts[0][1])

    def test_privacy_needs_policy_and_attestation_always_stops(self):
        with self.assertRaises(browser_form.NeedsReview):
            form, details, _ = browser_form.find_form(self.page, self.gate, self.root + "/privacy")
            self.fill(form, details)
        form, details, _ = browser_form.find_form(self.page, self.gate, self.root + "/privacy", privacy=True)
        self.assertEqual(self.fill(form, details)["consents"], ["I accept the privacy policy"])
        with self.assertRaises(browser_form.Assisted):
            browser_form.find_form(self.page, self.gate, self.root + "/attestation", privacy=True)
        self.assertEqual(Employer.posts, [])

    def test_access_checks_and_unapproved_action_require_assistance(self):
        for path in ("/bad", "/captcha", "/offsite", "/cross"):
            with self.subTest(path=path), self.assertRaises(browser_form.Assisted):
                browser_form.find_form(self.page, self.gate, self.root + path)
        self.assertEqual(Employer.posts, [])
        self.assertNotIn("unapproved", Employer.gets)

    def test_303_confirmation_is_an_explicit_guarded_get(self):
        form, details, _ = browser_form.find_form(self.page, self.gate, self.root + "/post303")
        outcome = browser_form.submit_form(self.page, self.gate, form, details, self.fill(form, details), Mock())
        self.assertEqual((outcome["state"], outcome["url"]), ("probable", self.root + "/thanks"))
        self.assertEqual(len(Employer.posts), 1)
        self.assertEqual(Employer.gets.count("thanks"), 1)

    def test_post_redirect_never_repeats_the_post(self):
        form, details, _ = browser_form.find_form(self.page, self.gate, self.root + "/replay")
        try:
            browser_form.submit_form(self.page, self.gate, form, details, self.fill(form, details), Mock())
        except PlaywrightError:
            pass
        self.assertEqual(len(Employer.posts), 1)
        self.assertTrue(self.gate.blocked_post)

    def test_browser_blocks_public_names_resolving_to_private_network(self):
        result = [(None, None, None, None, ("127.0.0.1", 443))]
        with patch("smartplier.generic_application.socket.getaddrinfo", return_value=result):
            with self.assertRaises(browser_form.PolicyBlocked):
                browser_form.Gate("https://jobs.example.org/apply")
        self.assertIsNone(browser_form.origin("https://in.indeed.com/posting"))

    def test_unknown_ats_preflight_to_probable_then_receipt(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path, profile_path, resume = root / "jobs.db", root / "profile.json", root / "smartplier.resume.html"
            resume.write_text("<!doctype html><h1>Fixture resume</h1>", encoding="utf8")
            profile = {**PROFILE, "basics": {**PROFILE["basics"], **self.basics}}
            profile_path.write_text(json.dumps(profile), encoding="utf8")
            prepare(path, profile_path)
            provider = Mock()
            provider.generate_json.return_value = {"fields": [
                {"id": "0", "key": "name"}, {"id": "1", "key": "email"}, {"id": "2", "key": "resume"},
            ]}
            work = SimpleNamespace(db=path, profile=profile_path, connection=lambda: database(path),
                                   profile_data=lambda: profile, provider=provider)
            job_url = self.root + "/custom"
            with database(path) as db, db:
                ident = add_job(db, "Python Developer", "Fixture Employer", "Remote", "Python", job_url)
                db.execute("UPDATE jobs SET score=99,score_method='gemini',status='to_apply' WHERE id=?", (ident,))
                stamp = analytics.timestamp()
                db.execute("""INSERT INTO job_discovery(job_id,source,first_seen,last_seen,published_at)
                              VALUES (?, 'Fixture', ?, ?, ?)""", (ident, stamp, stamp, stamp))
                auto_apply.single_row(db, get_job(db, ident))
            with database(path) as db:
                auto_apply.save_settings(db, {"enabled": True})
            gate_class = browser_form.Gate
            with patch("playwright.sync_api.sync_playwright", side_effect=lambda: nullcontext(self.driver)), patch(
                "smartplier.generic_application.origin", return_value="fixture"
            ), patch(
                "smartplier.generic_application.Gate", side_effect=lambda target: gate_class(target, fixture=True)
            ), patch("smartplier.auto_apply.generate_resume", return_value=([resume], [], False)):
                checked = auto_apply.preflight(work, ident)
                self.assertEqual(checked["status"], "ready", checked["detail"])
                self.assertEqual(Employer.posts, [])
                with database(path) as db:
                    self.assertEqual(auto_apply.recorded_preflight(db, ident)["ats"]["platform"], "Unknown")
                self.assertEqual(auto_apply.submit_selected(work, ident)["status"], "probable")
            self.assertEqual(len(Employer.posts), 1)
            self.assertEqual(provider.generate_json.call_count, 1)
            with database(path) as db:
                self.assertEqual(get_job(db, ident)["status"], "to_apply")
                artifacts = db.execute("SELECT page_html,screenshot FROM auto_diagnostics WHERE job_id=?", (ident,)).fetchall()
                self.assertEqual(len(artifacts), 2)
                self.assertTrue(all(row["page_html"] and row["screenshot"] for row in artifacts))
                self.assertNotIn("rotating-test-token", artifacts[0]["page_html"])
            with patch("smartplier.auto_apply.gmail_credentials", return_value=("fixture@example.test", "")), patch(
                "smartplier.confirmation_mail.find_receipts", return_value={ident: "fixture-receipt"}
            ):
                self.assertEqual(auto_apply.reconcile_receipts(work)["confirmed"], 1)
            with database(path) as db:
                self.assertEqual(get_job(db, ident)["status"], "applied")
