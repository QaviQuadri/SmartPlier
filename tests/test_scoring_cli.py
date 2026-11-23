"""Stage 1 regression tests. All Gemini responses are mocked."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from smartplier import jobhunt
from smartplier import scoring
from smartplier import tracker


PROFILE = json.loads((Path(__file__).parent / "fixtures" / "profile.example.json").read_text(encoding="utf-8"))
MOCK_RESULT = {
    "fit_score": 83, "matched": ["Python", "FastAPI"], "missing": ["PostgreSQL"],
    "red_flags": [], "ghost_job_risk": "unknown",
    "reason": "Python and FastAPI match; PostgreSQL is missing.",
}


def gemini_response(result):
    return io.BytesIO(json.dumps({
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "internal", "thought": True}, {"text": json.dumps(result)}]},
        }],
    }).encode())


class ScoringTests(unittest.TestCase):
    def test_explicit_locations(self):
        cases = {
            "Hyderabad, India": (1, None), "Secunderabad (Hybrid)": (1, None),
            "Bengaluru, India": (2, None), "Pune": (2, None), "Dubai, UAE": (3, None),
            "Riyadh, Saudi Arabia": (3, None), "Remote": (1, None),
            "Remote (India)": (1, None), "Remote - worldwide": (1, None),
            "Hyderabad / Mumbai": (1, None), "Remote (APAC)": (None, "review"),
            "": (None, "review"), "Anywhere": (None, "review"), "Hybrid": (None, "review"),
            "Remote - US only": (None, "filtered"), "Berlin, Germany": (None, "filtered"),
            "Hyderabad, Pakistan": (None, "filtered"), "London, UK": (None, "filtered"),
            "Paris, France / Toronto, Canada": (None, "filtered"),
            "Remote worldwide except India": (None, "review"),
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                self.assertEqual(scoring.location_decision(location, PROFILE["preferences"])[:2], expected)

    def test_deal_breakers(self):
        cases = [
            ("Signing bonus only applies during probation.", "us only", False),
            ("US only.", "us only", True),
            ("Remote, U.S. only.", "u.s. only", True),
            ("This role is not US only.", "us only", False),
            ("US only, not open globally.", "us only", True),
            ("No security clearance required.", "security clearance", False),
            ("Security clearance is not required.", "security clearance", False),
            ("No active security clearance is required.", "security clearance", False),
            ("No security clearance for interns, but security clearance is required for this role.", "security clearance", True),
            ("No degree required. Security clearance required.", "security clearance", True),
            ("5+ years preferred.", "5+ years", False),
            ("Requires 5+ years of experience.", "5+ years", True),
            ("Requires 15+ years of experience.", "5+ years", False),
        ]
        for posting, phrase, expected in cases:
            with self.subTest(posting=posting):
                self.assertEqual(scoring.has_deal_breaker(posting, phrase), expected)

    def test_contacts_are_extracted_without_network(self):
        contacts = scoring.extract_contacts(
            "Reach Jobs@Example.com, jobs@example.com or https://uk.linkedin.com/in/Recruiter/?trk=abc. "
            "See linkedin.com/in/Engineer and https://linkedin.com/company/example. "
            "Ignore bad-linkedin.com/in/false and evil.example/linkedin.com/in/false."
        )
        self.assertEqual(contacts, {
            "emails": ["jobs@example.com"],
            "linkedin_profiles": ["https://www.linkedin.com/in/engineer", "https://www.linkedin.com/in/recruiter"],
        })

    def test_location_never_comes_from_description(self):
        job = {"title": "AI Engineer", "location": "", "company": "Example",
               "description": "Remote. Hyderabad, India. Python and RAG."}
        evaluation = scoring.evaluate(job, PROFILE)
        self.assertEqual((evaluation.status, evaluation.method, evaluation.tier), ("review", "filter", None))
        job["location"] = "Dubai, UAE"
        with patch.object(scoring.GeminiProvider, "score", return_value=MOCK_RESULT):
            evaluation = scoring.evaluate(job, PROFILE, scoring.GeminiProvider("mock-key"))
        self.assertEqual((evaluation.tier, evaluation.details["fit_score"]), (3, 83))

    def test_profile_evidence_ids(self):
        bullets = [bullet["id"] for section in ("experience", "projects")
                   for entry in PROFILE[section] for bullet in entry["bullets"]]
        self.assertEqual(len(bullets), len(set(bullets)))
        for story in PROFILE["stories"]:
            self.assertTrue(set(story["evidence_ids"]).issubset(bullets))


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "test.sqlite3"
        env = patch.dict(os.environ, {"GEMINI_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args, success=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jobhunt.main(["--profile", str(Path(__file__).parent / "fixtures" / "profile.example.json"), "--db", str(self.db_path), *args])
        self.assertEqual(code, int(not success), err.getvalue())
        return out.getvalue(), err.getvalue()

    def add(self, description, location="Bengaluru, India"):
        return self.cli("add", "--title", "Python Developer", "--company", "Example",
                        "--location", location, "--description", description)

    def test_gemini_only_scoring_with_mocked_retries(self):
        basics = PROFILE["basics"]
        self.add(
            "Python, FastAPI, Redis and Docker. Recruiter: jobs@example.com, linkedin.com/in/recruiter. "
            f'Private data: {basics["name"]} {basics["email"]} {basics["phone"]} {basics["github"]}'
        )
        _, err = self.cli("score", success=False)
        self.assertIn("Set GEMINI_API_KEY", err)
        self.assertIn("#1 unscored - [-]", self.cli("list")[0])
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open",
            side_effect=[
                urllib.error.HTTPError(scoring.ENDPOINT, 429, "rate limit", {}, None),
                urllib.error.HTTPError(scoring.ENDPOINT, 503, "unavailable", {}, None),
                gemini_response(MOCK_RESULT),
            ],
        ) as request, patch("smartplier.scoring.time.sleep") as pause:
            out, _ = self.cli("score")
            self.assertEqual(out.strip(), "#1 to_apply 83/100 [gemini] | " + MOCK_RESULT["reason"])
            self.assertEqual(request.call_count, 3)
            self.assertEqual([call.args[0] for call in pause.call_args_list], [1, 2])
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, scoring.ENDPOINT)
            self.assertEqual(sent.get_header("X-goog-api-key"), "mock-key")
            payload = json.loads(sent.data)
            self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
            model_input = json.loads(payload["contents"][0]["parts"][0]["text"])
            self.assertEqual(set(model_input["candidate"]), {"skills", "seniority", "degrees", "evidence"})
            for token in (basics["name"], basics["email"], basics["phone"], basics["github"],
                          "jobs@example.com", "linkedin.com/in/recruiter", "mock-key",
                          "Lumen Finch Labs", "Lantern Lab"):
                self.assertNotIn(token, sent.data.decode())
            self.assertIn("No jobs need scoring.", self.cli("score")[0])
        shown, _ = self.cli("show", "1")
        for line in ("83 [gemini]", "unscored -> to_apply", "Missing: PostgreSQL", "Emails: casey@example.com, jobs@example.com"):
            self.assertIn(line, shown)

    def test_filter_short_circuit_and_id_rescore(self):
        self.add("Python Developer in Hyderabad", "")
        self.add("Must have security clearance")
        self.add("Python role", "Berlin, Germany")
        self.add("Python, Docker, FastAPI, Redis, SQL", "Remote")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open", side_effect=[gemini_response(MOCK_RESULT)],
        ) as request:
            out, _ = self.cli("score")
            self.assertIn("#1 review - [filter] | Location is missing.", out)
            self.assertIn("#2 filtered - [filter] | Deal-breaker: security clearance", out)
            self.assertIn("#3 filtered - [filter]", out)
            self.assertIn("#4 to_apply 83/100 [gemini]", out)
            self.assertEqual(request.call_count, 1)
        self.cli("status", "4", "applied", "--reason", "Sent manually.")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open", return_value=gemini_response(MOCK_RESULT),
        ):
            out, _ = self.cli("score", "--id", "4")
        self.assertIn("#4 applied 83/100 [gemini]", out)
        listing, _ = self.cli("list", "--status", "applied")
        self.assertEqual(len(listing.splitlines()), 1)
        self.assertIn("to_apply -> applied | Sent manually.", self.cli("show", "4")[0])

    def test_invalid_response_leaves_existing_score_intact_and_retryable(self):
        self.add("Python and FastAPI")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), patch(
            "urllib.request.OpenerDirector.open",
            side_effect=[gemini_response(MOCK_RESULT), gemini_response({**MOCK_RESULT, "fit_score": True}),
                         gemini_response({**MOCK_RESULT, "fit_score": 86})],
        ):
            self.cli("score")
            _, err = self.cli("score", "--id", "1", success=False)
            self.assertIn("invalid score", err)
            self.assertIn("83 [gemini]", self.cli("list")[0])
            self.assertIn("86/100 [gemini]", self.cli("score", "--id", "1")[0])

    def test_concurrent_change_prevents_stale_score_write(self):
        self.add("Python")
        with tracker.database(self.db_path) as first, tracker.database(self.db_path) as second:
            snapshot = tracker.get_job(first, 1)
            with patch.object(scoring.GeminiProvider, "score", return_value=MOCK_RESULT):
                result = scoring.evaluate(snapshot, PROFILE, scoring.GeminiProvider("mock-key"))
            tracker.change_status(second, 1, "applied", "Sent manually.")
            with self.assertRaisesRegex(ValueError, "changed during scoring"):
                tracker.save_score(first, snapshot, result)
            self.assertEqual(tracker.get_job(first, 1)["status"], "applied")
            self.assertIsNone(tracker.get_job(first, 1)["score_method"])


if __name__ == "__main__":
    unittest.main()
