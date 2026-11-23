"""Discovery adapter, storage, scheduler and credential regression tests."""

import ctypes
import imaplib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from smartplier import credentials
from smartplier import discovery
from smartplier import discovery_mail
from smartplier import discovery_sources as sources
from smartplier import jobhunt
from smartplier import scoring
from smartplier import tracker
from tests.test_scoring_cli import MOCK_RESULT, PROFILE, gemini_response


DESCRIPTION = "Develop Python and FastAPI services. " * 8
RSS = """<rss version="2.0" xmlns:h="https://himalayas.app" xmlns:c="http://purl.org/rss/1.0/modules/content/">
<channel><item><title>Python Developer</title><link>https://himalayas.app/jobs/python-1?utm_source=feed</link>
<guid>python-1</guid><h:companyName>Sample</h:companyName><h:locationRestriction>United States</h:locationRestriction>
<h:locationRestriction>India</h:locationRestriction><c:encoded><![CDATA[<p>Build APIs</p>]]></c:encoded>
<pubDate>Tue, 01 Jan 2024 12:00:00 GMT</pubDate><h:expiryDate>2024-01-02T12:00:00Z</h:expiryDate>
</item><item><title>AI Engineer</title><guid>worldwide</guid><link>https://himalayas.app/jobs/worldwide</link>
<description>Python and RAG.</description></item>
<item><title>Data Analyst</title><guid>unknown-location</guid><link>https://himalayas.app/jobs/region</link>
<h:timezoneRestriction>UTC-8</h:timezoneRestriction></item></channel></rss>"""


def message(sender, link, plain=False):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "fixture@example.org"
    mail["Subject"] = "Your job alert"
    if plain:
        mail.set_content(f"Job title: Python Developer\nCompany: Sample\nLocation: Hyderabad, India\n{link}")
    else:
        mail.set_content("HTML version.")
        mail.add_alternative(
            f'<a href="{link}">Python Developer</a><div>Company: Sample</div>'
            '<div>Location: Hyderabad, India</div><div>Build APIs.</div><div>Unsubscribe</div>',
            subtype="html",
        )
    return mail.as_bytes()


class AdapterTests(unittest.TestCase):
    def test_ats_endpoints_and_normalization(self):
        data = {
            "greenhouse": {"jobs": [{"id": 12, "title": "Python Developer", "location": {"name": "Hyderabad"},
                                     "absolute_url": "https://job-boards.greenhouse.io/sample/jobs/12?gh_src=feed",
                                     "content": "&lt;p&gt;Python&lt;/p&gt;&lt;script&gt;hidden&lt;/script&gt;"}]},
            "lever": [{"id": "41", "text": "AI Engineer",
                       "categories": {"allLocations": ["India", "Canada"]}, "workplaceType": "remote",
                       "hostedUrl": "https://jobs.lever.co/sample/41", "descriptionPlain": "Python role",
                       "lists": [{"text": "Required skills", "content": "<li>FastAPI</li>"}], "createdAt": 1704100000000}],
            "ashby": {"jobs": [{"title": "ML Engineer", "location": "Bengaluru, India",
                                "secondaryLocations": [{"location": "Dubai"}], "descriptionPlain": "Role",
                                "jobUrl": "https://jobs.ashbyhq.com/sample/12", "publishedAt": "2024-01-01"},
                               {"title": "Hidden", "isListed": False}]},
            "workable": {"jobs": [{"shortcode": "ABC", "title": "Data Analyst",
                                   "location": {"city": "Dubai", "country": "United Arab Emirates"},
                                   "description": "<p>SQL</p>", "shortlink": "https://apply.workable.com/j/ABC"}]},
        }
        expected = {
            "greenhouse": ("Hyderabad", "Python"),
            "lever": ("Remote - India / Remote - Canada", "FastAPI"),
            "ashby": ("Bengaluru, India / Dubai", "Role"),
            "workable": ("Dubai, United Arab Emirates", "SQL"),
        }
        for provider, payload in data.items():
            with self.subTest(provider=provider), patch("smartplier.discovery_sources.fetch", return_value=payload) as fetch:
                batch = sources.ats_batch({"provider": provider, "board": "sample", "company": "Sample"})
                fetch.assert_called_once_with(sources.BOARDS[provider].format(board="sample"), "json")
                self.assertEqual(len(batch.postings), 1)
                post = batch.postings[0]
                self.assertEqual(post.location, expected[provider][0])
                self.assertIn(expected[provider][1], post.description)
                self.assertNotIn("gh_src", post.url)
                self.assertNotIn("hidden", post.description)

    def test_rss_and_adzuna(self):
        with patch("smartplier.discovery_sources.fetch", return_value=sources.ET.fromstring(RSS)):
            feed = sources.rss_batch({"name": "Himalayas", "url": "https://himalayas.app/jobs/rss"})
        self.assertEqual([item.location for item in feed.postings],
                         ["Remote - United States / Remote - India", "Remote worldwide", "Remote (timezone restrictions)"])
        self.assertEqual(feed.postings[0].description, "Build APIs")
        self.assertEqual(feed.postings[0].expires_at.day, 2)
        example = {"id": "456", "title": "Data Analyst", "company": {"display_name": "Sample"},
                   "location": {"area": ["India", "Telangana", "Hyderabad"]}, "description": "Python",
                   "redirect_url": "https://www.adzuna.in/details/456?app_key=mock-value&utm_source=feed"}
        with patch("smartplier.discovery_sources.fetch", return_value={"results": [example]}) as fetch:
            batch = sources.adzuna_batch({"queries": ["data"], "pages": 2, "results_per_page": 20},
                                         "mock-id", "mock-value")
        fetch.assert_called_once()
        self.assertIn("/jobs/in/search/1?", fetch.call_args.args[0])
        self.assertEqual(batch.postings[0].location, "Hyderabad, Telangana, India")
        self.assertEqual(batch.postings[0].url, "https://www.adzuna.in/details/456")

    def test_fetch_blocks_unapproved_hosts_and_sanitizes_network_errors(self):
        for url in ("https://www.linkedin.com./jobs/123", "https://alerts.indeed.com/feed.xml",
                    "https://www.naukri.com/feed", "http://himalayas.app/jobs/rss"):
            with self.subTest(url=url), patch("smartplier.discovery_sources.build_opener") as transport:
                with self.assertRaisesRegex(sources.SourceError, "permitted sites"):
                    sources.fetch(url, "xml")
                transport.assert_not_called()
        url = "https://api.adzuna.com/v1/api/jobs/in/search/1?app_key=mock-secret"
        with patch("urllib.request.OpenerDirector.open", side_effect=HTTPError(url, 429, "mock-secret", {}, None)):
            with self.assertRaisesRegex(sources.SourceError, r"^Source returned HTTP 429\.$"):
                sources.fetch(url, "json")

    def test_alert_formats_and_imap_read_only(self):
        cases = {
            "linkedin.com": "https://www.linkedin.com/comm/jobs/view/python-developer-1234567/?trk=feed",
            "indeed.com": "https://in.indeed.com/rc/clk?jk=abcd1234",
            "naukri.com": "https://www.naukri.com/job-listings-python-developer-sample-123456",
            "bayt.com": "https://www.bayt.com/en/uae/jobs/python-developer-123456/",
            "gulftalent.com": "https://www.gulftalent.com/uae/jobs/python-developer-123456",
            "naukrigulf.com": "https://www.naukrigulf.com/python-developer-jobs-in-dubai-123456",
        }
        for host, link in cases.items():
            for plain in (False, True):
                with self.subTest(host=host, plain=plain):
                    alert, = discovery_mail.alert_postings(message(f"alerts@{host}", link, plain))
                    self.assertEqual((alert.title, alert.company, alert.location), ("Python Developer", "Sample", "Hyderabad, India"))
                    self.assertNotIn("Unsubscribe", alert.description)
        self.assertEqual(discovery_mail.alert_postings(message("bad@notlinkedin.com", cases["linkedin.com"])), [])
        self.assertEqual(discovery_mail.alert_postings(message(
            "alerts@linkedin.com", "https://example.org/?url=https://www.linkedin.com/jobs/view/1234567",
        )), [])
        inbox = MagicMock()
        inbox.select.return_value = ("OK", [b"2"])
        data = message("alerts@linkedin.com", cases["linkedin.com"])
        inbox.uid.side_effect = [("OK", [b"101 102"]), ("OK", [(b"MIME", data)])]
        with patch("smartplier.discovery_mail.imaplib.IMAP4_SSL") as transport:
            transport.return_value.__enter__.return_value = inbox
            batch = discovery_mail.gmail_batch({"mailbox": "INBOX", "days": 14, "limit": 1},
                                               "mail@example.org", "abcd efgh ijkl mnop")
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args, ("imap.gmail.com", 993))
        inbox.select.assert_called_once_with('"INBOX"', readonly=True)
        inbox.login.assert_called_once_with("mail@example.org", "abcdefghijklmnop")
        self.assertIn("X-GM-RAW", inbox.uid.call_args_list[0].args)
        self.assertEqual(inbox.uid.call_args_list[1].args[1], b"102")
        self.assertIn("BODY.PEEK[]", inbox.uid.call_args_list[1].args[2])
        self.assertEqual(len(batch.postings), 1)

    def test_gmail_errors_identify_stage_without_echoing_server_data(self):
        options = {"mailbox": "INBOX", "days": 14, "limit": 1}
        with patch("smartplier.discovery_mail.imaplib.IMAP4_SSL") as transport:
            with self.assertRaisesRegex(sources.SourceError, "16-character app password"):
                discovery_mail.gmail_batch(options, "mail@example.org", "invalid")
            transport.assert_not_called()
            transport.side_effect = TimeoutError("private transport details")
            with self.assertRaisesRegex(sources.SourceError, r"failed while trying to connect\.$"):
                discovery_mail.gmail_batch(options, "mail@example.org", "abcdefghijklmnop")
            transport.side_effect = None
            inbox = transport.return_value.__enter__.return_value
            inbox.login.side_effect = imaplib.IMAP4.error("private server details")
            with self.assertRaisesRegex(sources.SourceError, r"^Gmail rejected the sign-in\."):
                discovery_mail.gmail_batch(options, "mail@example.org", "abcdefghijklmnop")
            inbox.select.assert_not_called()
            inbox.login.side_effect = None
            inbox.select.return_value = ("OK", [])
            inbox.uid.side_effect = imaplib.IMAP4.error("private search diagnostics")
            with self.assertRaisesRegex(sources.SourceError, r"^Gmail IMAP could not search for job alerts\.$"):
                discovery_mail.gmail_batch(options, "mail@example.org", "abcdefghijklmnop")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "test.sqlite3"
        self.config = Path(temp.name) / "discovery.json"
        self.config.write_text('{"rss":[{"name":"Himalayas","url":"https://himalayas.app/jobs/rss"}]}', encoding="utf-8")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jobhunt.main(["--profile", str(Path(__file__).parent / "fixtures" / "profile.example.json"), "--db", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def test_discover_twice_then_explicit_mocked_gemini(self):
        with patch("smartplier.discovery_sources.fetch", return_value=sources.ET.fromstring(RSS)), patch(
            "urllib.request.OpenerDirector.open"
        ) as gemini:
            first = self.cli("discover", "--config", str(self.config))
            self.assertEqual(first[:2], (0, "Himalayas: 3 added, 0 duplicates, 1 need review, "
                                        "0 filtered, 0 outside target roles, 0 skipped.\n"))
            self.assertIn("0 added, 3 duplicates", self.cli("discover", "--config", str(self.config))[1])
            gemini.assert_not_called()
        detail = self.cli("show", "1")[1]
        self.assertIn("Source: Himalayas", detail)
        self.assertIn("stated expiry date has passed", detail)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open",
            side_effect=[gemini_response(MOCK_RESULT), gemini_response(MOCK_RESULT)],
        ) as gemini:
            code, out, _ = self.cli("score")
            self.assertEqual(code, 0)
            self.assertEqual(gemini.call_count, 2)
            self.assertIn("#1 to_apply 83/100 [gemini]", out)
            self.assertIn("#2 to_apply 83/100 [gemini]", out)

    def test_manual_dedupe_role_filter_repost_and_status_preservation(self):
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        one = sources.make_posting("Python Developer", "Sample", "Hyderabad", DESCRIPTION,
                                   "https://jobs.example.org/one?utm_campaign=board", "one", "2024-06-01")
        two = sources.make_posting("Python Developer", "Sample", "Hyderabad", DESCRIPTION,
                                   "https://jobs.example.org/two", "two", "2024-10-01")
        filtered = sources.make_posting("Account Manager", "Sample", "Hyderabad", "Sales", "https://jobs.example.org/three", "three")
        with tracker.database(self.db) as db:
            job_id = tracker.add_job(db, one.title, one.company, one.location, one.description,
                                     "https://jobs.example.org/one?utm_source=email")
            tracker.change_status(db, job_id, "applied", "Sent manually.")
            for imports in [[one], [two, filtered], [two, filtered]]:
                counts = discovery.import_batch(db, sources.SourceBatch("greenhouse:sample", "Sample board", imports),
                                                PROFILE, now=now)
            self.assertEqual(counts["duplicates"], 1)
            self.assertEqual(counts["off_target"], 1)
            jobs = db.execute("SELECT status, score_method FROM jobs ORDER BY id").fetchall()
            self.assertEqual([tuple(job) for job in jobs], [("applied", None)])
            metadata = discovery.discovery_summary(db.execute("SELECT * FROM job_discovery WHERE job_id = 1").fetchone(), now)
            self.assertEqual(metadata["published"], "2024-10-01")
            self.assertIn("Possible reposts detected: 1.", metadata["flags"])

    def test_failed_source_continues_to_next_one_and_scheduler_repeats(self):
        self.config.write_text('{"rss":[{"name":"Failed","url":"https://bad.example.org/feed"},'
                               '{"name":"Next","url":"https://himalayas.app/jobs/rss"}]}', encoding="utf-8")
        with patch("smartplier.discovery_sources.fetch", side_effect=[sources.SourceError("Source returned HTTP 503."),
                                                            sources.ET.fromstring(RSS)]):
            code, output, error = self.cli("discover", "--config", str(self.config))
        self.assertEqual(code, 1)
        self.assertIn("Next: 3 added", output)
        self.assertIn("Failed: Source returned HTTP 503.", error)
        with patch("smartplier.discovery.discovery_run", return_value=0) as run, patch(
            "smartplier.discovery.time.sleep", side_effect=[None, KeyboardInterrupt],
        ) as sleep:
            self.assertEqual(self.cli("discover", "--interval-minutes", "5")[0], 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [300, 300])


class CredentialTests(unittest.TestCase):
    def test_native_store_round_trip_through_mocked_windows_calls(self):
        vault = MagicMock()
        stored = {}
        memory = []

        def write(pointer, flags):
            entry = ctypes.cast(pointer, credentials.CREDENTIAL_PTR).contents
            self.assertEqual((entry.TargetName, entry.Persist, flags), (credentials.TARGET, 2, 0))
            stored["key"] = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize)
            return True

        def read(target, kind, flags, out):
            self.assertEqual((target, kind, flags), (credentials.TARGET, 1, 0))
            buf = (ctypes.c_ubyte * len(stored["key"])).from_buffer_copy(stored["key"])
            entry = credentials.Credential(CredentialBlobSize=len(buf), CredentialBlob=buf)
            memory.extend((buf, entry))
            ctypes.cast(out, ctypes.POINTER(credentials.CREDENTIAL_PTR))[0] = ctypes.pointer(entry)
            return True

        vault.CredWriteW.side_effect = write
        vault.CredReadW.side_effect = read
        with patch.dict(os.environ, {}, clear=True), patch("smartplier.credentials.os.name", "nt"), patch(
            "smartplier.credentials.windows_vault", return_value=vault,
        ), patch("smartplier.credentials.getpass.getpass", return_value="mock-key"):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(jobhunt.main(["setup-gemini"]), 0)
            self.assertNotIn("mock-key", out.getvalue())
            self.assertEqual(out.getvalue().strip(), "Gemini key saved in Windows Credential Manager.")
            with patch.object(scoring, "GeminiProvider") as provider:
                scoring.gemini_from_settings()
                provider.assert_called_once_with("mock-key")
            vault.CredFree.assert_called_once()
            with patch.dict(os.environ, {"GEMINI_API_KEY": "environment-key"}):
                self.assertEqual(credentials.gemini_key(), "environment-key")
            vault.CredReadW.assert_called_once()


if __name__ == "__main__":
    unittest.main()
