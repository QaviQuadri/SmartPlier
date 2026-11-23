import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from smartplier import analytics
from smartplier.form_engine import NeedsReview, fill_fields
from smartplier.generic_application import Gate
from smartplier.auto_apply import execute, reserve, save_settings
from smartplier.tracker import add_job, database, get_job
from smartplier.web_service import prepare
from tests.test_scoring_cli import PROFILE

TARGET = "https://jobs.lever.co/example/781ee264-00f1-46db-a523-8f31ee4d6541/apply"


def field(name, kind, label="", required=True):
    return {"name": name, "type": kind, "label": label, "required": required, "visible": True, "disabled": False}


class AdapterTests(unittest.TestCase):
    def test_unknown_and_legal_fields_stop_before_filling(self):
        for item in (field("custom", "text", "Favorite colour"), field("custom", "text", "Work authorization")):
            form = MagicMock()
            form.locator.return_value.count.return_value = 1
            profile = dict(PROFILE, verified_screening_answers={"work authorization": "Yes"})
            with self.assertRaises(NeedsReview):
                fill_fields(form, [field("name", "text"), field("resume", "file"), item],
                            PROFILE["basics"], profile, {"name": "Resume.pdf"})
            form.locator.return_value.fill.assert_not_called()
            form.locator.return_value.set_input_files.assert_not_called()

    def test_network_allows_only_one_armed_submission(self):
        url = "http://127.0.0.1:8765/apply"
        gate = Gate(url, fixture=True)
        route = MagicMock()
        route.request.url = url
        route.request.redirected_from = None
        route.request.method = "POST"
        route.request.is_navigation_request.return_value = True
        with patch.object(gate, "forward") as forward:
            gate.route(route)
            forward.assert_not_called()
            gate.endpoint = url
            gate.route(route)
            gate.route(route)
            forward.assert_called_once_with(route, route.request)
        self.assertEqual(route.abort.call_count, 2)


class EngineTests(unittest.TestCase):
    def test_confirmation_transaction_and_uncertain_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jobs.sqlite3"
            profile_file = Path(folder) / "profile.json"
            profile_file.write_text(json.dumps(PROFILE), encoding="utf-8")
            prepare(path, profile_file)
            work = SimpleNamespace(db=path, profile=profile_file, provider=None,
                                   profile_data=lambda: PROFILE, connection=lambda: database(path))
            with database(path) as db:
                save_settings(db, {"enabled": True, "daily_cap": 2})
                ids = []
                for index in range(2):
                    url = TARGET.replace("781e", f"781{index}")
                    ident = add_job(db, ("Python Developer", "Backend Developer")[index], "Example", "Remote", "Python", url)
                    ids.append(ident)
                    stamp = analytics.timestamp()
                    with db:
                        db.execute("UPDATE jobs SET status='to_apply',score_method='gemini',score=90,scored_at=? WHERE id=?", (stamp,ident))
                        db.execute("INSERT INTO job_discovery(job_id,source,first_seen,last_seen,published_at) VALUES (?,?,?,?,?)", (ident,"Example (lever)",stamp,stamp,stamp))
                        db.execute("INSERT INTO auto_applications(job_id,identity,status,updated_at,detail) VALUES (?,?,?,?,?)", (ident,url,"queued",stamp,"Queued"))
            def send(work, job, profile, html):
                reserve(work, job["id"], job["url"], "123456")
                if job["id"] == ids[1]:
                    raise RuntimeError("mock disconnect")
                return {"state": "confirmed", "confirmation": "Thank you for applying", "url": job["url"]}, "123456"
            with patch("smartplier.auto_apply.generate_resume", return_value=([Path("smartplier.resume.html")], [], False)), patch("smartplier.auto_apply.deliver", side_effect=send):
                self.assertEqual(execute(work, ids[0])["status"], "submitted")
                with self.assertRaisesRegex(ValueError, "Submission may have occurred"):
                    execute(work, ids[1])
            with database(path) as db:
                self.assertEqual(get_job(db, ids[0])["status"], "applied")
                self.assertIsNotNone(analytics.application(db, ids[0]))
                self.assertEqual(get_job(db, ids[1])["status"], "to_apply")
                row = db.execute("SELECT status,attempted_day FROM auto_applications WHERE job_id=?", (ids[1],)).fetchone()
                self.assertEqual(row["status"], "needs_review")
                self.assertIsNotNone(row["attempted_day"])
            with self.assertRaises(NeedsReview):
                reserve(work, ids[1], TARGET, "123456")
