"""Resume generation, validation, cache and score migration tests."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from smartplier import jobhunt
from smartplier import resume
from smartplier import tracker
from smartplier.resume_templates import latex_text
from tests.test_scoring_cli import gemini_response


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile_path = self.root / "profile.json"
        self.db_path = self.root / "jobs.sqlite3"
        self.profile = json.loads((Path(__file__).parent / "fixtures" / "profile.example.json").read_text(encoding="utf-8"))
        self.write_profile()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}).start()
        self.evidence = resume.evidence_index(self.profile)
        self.mock_result = {
            "summary": self.profile["basics"]["summary"] + " Unverified claim about tenure.",
            "skills": ["Python", "FastAPI", "Kubernetes"],
            "bullets": [
                {"id": "missing-id", "text": "Unverified employer."},
                {"id": "lumen-pipelines", "text": self.evidence["lumen-pipelines"].replace("30%", "90%")},
                {"id": "lumen-models", "text": self.evidence["lumen-models"].replace("XGBoost", "LightGBM")},
                {"id": "harbor-rag", "text": self.evidence["harbor-rag"].replace("Built", "Developed", 1)},
            ],
        }
        private = self.profile["basics"]
        self.cli("add", "--title", "AI Engineer", "--company", "Example", "--location", "Dubai",
                 "--description", f'Python and RAG. Contact {private["email"]}. <script>posting</script>')

    def write_profile(self):
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")

    def cli(self, *args, expected_code=0):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = jobhunt.main(["--db", str(self.db_path), "--profile", str(self.profile_path), *args])
        self.assertEqual(code, expected_code, errors.getvalue())
        return output.getvalue(), errors.getvalue()

    def test_one_mocked_request_validates_and_caches_resume(self):
        with patch("urllib.request.OpenerDirector.open", return_value=gemini_response(self.mock_result)) as api:
            out, _ = self.cli("resume", "1")
            self.assertIn("Generated resume for job #1.", out)
            self.assertIn("Kept lumen-pipelines: the rewrite changed a number.", out)
            self.assertIn("Kept lumen-models: the rewrite changed wording", out)
            self.assertIn("Dropped 1 unknown bullet selection(s).", out)
            self.assertIn("Dropped 1 skill selection(s) outside the profile.", out)
            self.assertIn("Dropped summary text", out)
            request = api.call_args.args[0]
            payload = json.loads(request.data)
            private = self.profile["basics"]
            for field in ("name", "email", "phone", "github", "linkedin"):
                self.assertNotIn(private[field], request.data.decode())
            for value in ("Lumen Finch Labs", "Lantern Lab", "mock-key"):
                self.assertNotIn(value, request.data.decode())
            self.assertIn("bullets", payload["generationConfig"]["responseJsonSchema"]["properties"])
            html_path, tex_path = resume.resume_paths(self.db_path, self.profile_path, 1)
            html = html_path.read_text(encoding="utf-8")
            tex = tex_path.read_text(encoding="utf-8")
            for content in (html, tex):
                self.assertIn(private["email"], content)
                self.assertIn("Developed RAG-based alert", content)
                for forbidden in ("90%", "LightGBM", "Kubernetes", "Unverified", "<script>"):
                    self.assertNotIn(forbidden, content)
            self.assertIn("30%", html)
            self.assertIn(r"30\%", tex)
            self.assertIn(r"\documentclass", tex)

            tex_path.unlink()
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
                cached, _ = self.cli("resume", "1")
                self.assertIn("Cached resume for job #1.", cached)
                self.assertIn("Dropped 1 unknown bullet selection(s).", cached)
            self.assertEqual(tex_path.read_text(encoding="utf-8"), tex)
            self.assertEqual(api.call_count, 1)

    def test_cache_invalidates_for_requested_inputs_only(self):
        def generate(*args, **kwargs):
            return gemini_response(self.mock_result)

        with patch("urllib.request.OpenerDirector.open", side_effect=generate) as api:
            self.cli("resume", "1")
            self.profile["basics"]["email"] = "updated@example.com"
            self.write_profile()
            self.assertIn("Cached", self.cli("resume", "1")[0])
            html, _ = resume.resume_paths(self.db_path, self.profile_path, 1)
            self.assertIn("updated@example.com", html.read_text(encoding="utf-8"))
            self.assertEqual(api.call_count, 1)

            self.cli("resume", "1", "--force")
            with tracker.database(self.db_path) as db, db:
                db.execute("UPDATE jobs SET description = 'Changed posting' WHERE id = 1")
            self.cli("resume", "1")
            self.profile["skills"].append("test skill")
            self.write_profile()
            self.cli("resume", "1")
            with patch("smartplier.resume.TEMPLATE_VERSION", "test-template"):
                self.cli("resume", "1")
                with patch("smartplier.scoring.GeminiProvider.model", "test-model"):
                    self.cli("resume", "1")
                    self.assertIn("Cached", self.cli("resume", "1")[0])
            self.assertEqual(api.call_count, 6)

    def test_summary_cap_and_tex_escaping(self):
        self.profile["basics"]["summary"] = ("A" * 200) + ". " + ("B" * 200) + "."
        selection = resume.validate_selection({**self.mock_result, "summary": self.profile["basics"]["summary"]},
                                              self.profile)
        self.assertEqual(len(selection["summary"]), 201)
        self.assertIn("Shortened the summary to fit.", selection["warnings"])
        self.assertEqual(latex_text(r"\input{test} & 2% # _ $ ~ ^ < >"),
                         r"\textbackslash{}input\{test\} \& 2\% \# \_ \$ \textasciitilde{} \textasciicircum{} \textless{} \textgreater{}")

    def test_no_key_can_only_use_existing_resume_cache(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            _, error = self.cli("resume", "1", expected_code=1)
        self.assertIn("Set GEMINI_API_KEY", error)
        self.assertFalse(resume.resume_paths(self.db_path, self.profile_path, 1)[0].exists())

    def test_migration_discards_offline_scores_and_restores_gemini_scores(self):
        with sqlite3.connect(self.db_path) as db:
            for _ in range(3):
                db.execute("""INSERT INTO jobs(title, company, location, description, url, contacts, status)
                              SELECT title, company, location, description, url, contacts, status FROM jobs WHERE id=1""")
            db.execute("ALTER TABLE jobs ADD COLUMN location_penalty INTEGER")
            db.execute("UPDATE jobs SET status='to_apply', score_method='offline', score=72 WHERE id=1")
            db.execute("UPDATE jobs SET status='applied', score_method='offline', score=72 WHERE id=2")
            db.execute("""UPDATE jobs SET status='low', score_method='gemini', score=60,
                          location_penalty=15, score_details=? WHERE id=3""",
                       (json.dumps({"raw_fit_score": 75, "fit_score": 60}),))
            db.execute("UPDATE jobs SET status='filtered', score_method='filter', score=0 WHERE id=4")
            db.execute("PRAGMA user_version = 0")
        with tracker.database(self.db_path, self.profile_path) as db:
            states = db.execute("SELECT status, score_method, score FROM jobs ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in states], [
                ("unscored", None, None), ("applied", None, None),
                ("to_apply", "gemini", 75), ("filtered", "filter", None),
            ])
            self.assertIn("Gemini-only", tracker.history(db, 3)[0]["reason"])
        with tracker.database(self.db_path, self.profile_path) as db:
            self.assertEqual(len(tracker.history(db, 3)), 1)


if __name__ == "__main__":
    unittest.main()
