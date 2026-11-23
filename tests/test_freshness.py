import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from smartplier import analytics
from smartplier import auto_apply
from smartplier import freshness
from smartplier.form_engine import NeedsReview, PolicyBlocked
from tests.test_scoring_cli import PROFILE
from smartplier.tracker import add_job, database, get_job
from smartplier.web_service import Workspace, prepare


TARGET = "https://apply.workable.com/fableworks-demo/j/DEMO678901/apply/"
CODE = "DEMO678901"


class OfficialBoardTests(unittest.TestCase):
    def test_active_vs_closed_workable(self):
        job = {"url": "https://apply.workable.com/j/" + CODE}
        aliases = ["id:workable:fableworks-demo:" + CODE]
        with patch("smartplier.freshness.fetch", return_value={"jobs":[{"shortcode": CODE, "state":"Telangana", "application_url":TARGET}]}), patch("smartplier.freshness.application_access", return_value=TARGET) as access:
            verified = freshness.verify(job, aliases)
            self.assertIn("official Workable", verified.message)
            self.assertEqual(verified.application_url, TARGET)
            access.assert_called_once_with(TARGET, "apply.workable.com")
        for data in ({"jobs":[]}, {"jobs":[{"shortcode":CODE,"state":"closed"}]}):
            with patch("smartplier.freshness.fetch", return_value=data), patch("smartplier.freshness.application_access") as access:
                with self.assertRaises(PolicyBlocked):
                    freshness.verify(job, aliases)
                access.assert_not_called()


class FreshnessTests(unittest.TestCase):
    def test_older_supported_job_requires_reason_then_gets_ready(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, html = root / "jobs.sqlite3", root / "smartplier.resume.html"
            html.write_text("Mock resume", encoding="utf-8")
            prepare(path)
            work = SimpleNamespace(db=path, profile=root / "profile.json", provider=None,
                                   connection=lambda: database(path), profile_data=lambda: PROFILE)
            url = "https://jobs.lever.co/sample/781ee264-00f1-46db-a523-8f31ee4d6541/apply"
            with database(path) as db:
                ident = add_job(db, "Python Developer", "Sample", "Remote", "Python", url)
                auto_apply.save_settings(db, {"enabled": True})
                with db:
                    db.execute("UPDATE jobs SET score=90,score_method='gemini',status='to_apply' WHERE id=?", (ident,))
                    stamp = analytics.timestamp()
                    published = analytics.timestamp(analytics.date(stamp) - timedelta(days=26))
                    db.execute("INSERT INTO job_discovery(job_id,source,first_seen,last_seen,published_at) VALUES (?,?,?,?,?)",
                               (ident, "Sample (lever)", stamp, stamp, published))
                    auto_apply.single_row(db, get_job(db, ident))
            with patch("smartplier.auto_apply.active_posting", return_value=freshness.Verification("Official board and page verified", url)), patch(
                "smartplier.auto_apply.generate_resume", return_value=([html], [], False)
            ), patch("smartplier.auto_apply.inspect_form", return_value={"fields": ["Name", "Email"], "unanswered": []}), patch("smartplier.auto_apply.deliver") as submit:
                first = auto_apply.preflight(work, ident)
                self.assertEqual(first["status"], "needs_review")
                self.assertIn("Record a reason", first["detail"])
                with database(path) as db:
                    auto_apply.save_override(db, ident, "Verified active opening")
                self.assertEqual(auto_apply.preflight(work, ident)["status"], "ready")
                with database(path) as db:
                    result = auto_apply.check_preflight(db, get_job(db, ident), PROFILE)
                    self.assertEqual(result["fields"], ["Name", "Email"])
                    self.assertEqual(result["unanswered"], [])
                submit.assert_not_called()

    def test_selected_job_old_active_override_and_no_other_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            db_path=root/"jobs.sqlite3"
            profile=root/"profile.json"
            profile.write_text(json.dumps(PROFILE),encoding="utf-8")
            prepare(db_path,profile)
            now=analytics.date(analytics.timestamp())
            ids=[]
            with database(db_path) as db:
                for title,code in (("Generative AI Engineer",CODE),("Hiring Generative AI Engineer","DEMO123456")):
                    ident=add_job(db,title,"Fableworks Demo","Hyderabad","Python","https://apply.workable.com/j/"+code)
                    ids.append(ident)
                    with db:
                        db.execute("INSERT INTO job_discovery(job_id,source,first_seen,last_seen,published_at) VALUES (?,?,?,?,?)",
                                   (ident,"Fableworks Demo (workable)",analytics.timestamp(),analytics.timestamp(),analytics.timestamp(now-timedelta(days=26))))
                auto_apply.save_override(db,ids[0],"Still listed by the employer")
            with patch("smartplier.web_service.gmail_credentials", return_value=("","")), patch(
                    "smartplier.auto_apply.active_posting", return_value=freshness.Verification("Verified official Workable board", TARGET)
            ) as verify, patch("smartplier.auto_apply.inspect_form",side_effect=NeedsReview("Fixture form could not be inspected")), patch("smartplier.auto_apply.deliver") as network:
                work=Workspace(db_path,profile,None)
                try:
                    auto_apply.queue_single(work,ids[0])
                    for _ in range(200):
                        if work.snapshot()[-1]["status"] not in ("queued","running"):
                            break
                        time.sleep(.01)
                    verify.assert_called_once()
                    network.assert_not_called()
                    with work.connection() as db:
                        summary=auto_apply.recorded_preflight(db,ids[0])
                        self.assertTrue(summary["verified"])
                        self.assertEqual(summary["age"],26)
                        self.assertEqual(summary["override"]["reason"],"Still listed by the employer")
                        self.assertFalse(summary["ready"])
                        self.assertFalse(summary["inspected"])
                        self.assertIsNone(auto_apply.recorded_preflight(db,ids[1]))
                        self.assertEqual(db.execute("SELECT status FROM auto_applications WHERE job_id=?", (ids[0],)).fetchone()[0],"needs_review")
                        with db:
                            db.execute("UPDATE auto_applications SET attempted_day='2020-01-01' WHERE job_id=?", (ids[0],))
                    with self.assertRaises(PolicyBlocked):
                        auto_apply.queue_single(work,ids[1])
                finally:
                    work.close()

    def test_missing_date_and_expiry_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"jobs.sqlite3"
            prepare(path)
            with database(path) as db:
                ident=add_job(db,"AI Engineer","Example","Hyderabad","Python","")
                job=get_job(db,ident)
                auto_apply.save_override(db,ident,"Reviewed URL")
                with self.assertRaisesRegex(NeedsReview,"unambiguous publication date"):
                    auto_apply.verify_freshness(db,job,auto_apply.DEFAULTS)
                with db:
                    db.execute("INSERT INTO job_discovery(job_id,source,first_seen,last_seen,expires_at) VALUES (?,?,?,?,?)",
                               (ident,"Workable",analytics.timestamp(),analytics.timestamp(),"2020-01-01T00:00:00+00:00"))
                with self.assertRaisesRegex(PolicyBlocked,"expired"):
                    auto_apply.verify_freshness(db,job,auto_apply.DEFAULTS)
