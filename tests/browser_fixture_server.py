"""Isolated server used by the Playwright dashboard test."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from smartplier import auto_apply
from smartplier import screening
from smartplier.dashboard import DashboardServer
from smartplier.discovery_sources import SourceBatch, make_posting
from tests.test_scoring_cli import MOCK_RESULT, PROFILE
from smartplier.tracker import add_job, database, get_job
from smartplier.web_app import WebHandler
from smartplier.web_service import Workspace, prepare


class MockGemini:
    method = "gemini"
    model = "gemini-3.1-flash-lite"

    def score(self, _payload):
        return MOCK_RESULT

    def generate_json(self, instructions, _payload, schema):
        if "evidence_id" in schema["properties"]:
            return {"evidence_id": "lumen-pipelines"}
        return {"summary": PROFILE["basics"]["summary"], "skills": ["python"],
                "bullets": [{"id": "lumen-pipelines", "text": PROFILE["experience"][0]["bullets"][0]["text"]}]}


def main():
    with TemporaryDirectory() as folder, patch("smartplier.web_service.gmail_credentials", return_value=("", "")):
        db, profile = (Path(folder) / name for name in ("browser.sqlite3", "profile.json"))
        sample = {**PROFILE, "basics": {**PROFILE["basics"], "name": "Sample Candidate",
                                       "email": "candidate@example.org", "phone": "",
                                       "github": "", "linkedin": ""}}
        profile.write_text(json.dumps(sample), encoding="utf-8")
        (Path(folder) / "discovery.json").write_text(json.dumps({
            "companies": [{"provider": "lever", "board": "test", "company": "Browser Source"}], "rss": [],
        }), encoding="utf-8")
        prepare(db, profile)
        provider = MockGemini()
        with database(db) as conn, conn:
            ident = add_job(conn, "Sample screening job", "Fixture Employer", "Remote", "Python",
                            "https://example.org/screening")
            job = dict(get_job(conn, ident))
            auto_apply.single_row(conn, job)
            auto_apply.update(conn, ident, "needs_review", "Waiting for a verified screening answer.")
        questions = screening.Session(SimpleNamespace(connection=lambda: database(db), provider=provider),
                                      job, sample, "Fixture ATS", plan=True)
        field = {"label": "Which shift do you prefer?", "type": "select-one",
                 "options": [{"label": label, "value": label} for label in ("Morning", "Evening")]}
        questions.resolve(field, [field], True)
        batch = SourceBatch("lever:test", "Browser Source (lever)", [
            make_posting("Python Engineer", "Fixture", "Remote", "Python", "https://example.org/job", "fixture")
        ])
        with patch("smartplier.web_service.ats_batch", return_value=batch), DashboardServer(0, db, profile, provider, WebHandler) as server:
            server.workspace = Workspace(db, profile, provider)
            print(server.origin, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.workspace.close()


if __name__ == "__main__":
    main()
