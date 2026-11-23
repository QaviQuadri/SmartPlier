"""Fictional, temporary dashboard workspace for demos and documentation."""
import json
import shutil
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from smartplier import analytics, auto_apply, outreach, resume, screening
from smartplier.dashboard import DashboardServer, HttpError
from smartplier.tracker import add_job, database, get_job, save_score
from smartplier.scoring import evaluate
from smartplier.web_app import DIST, WebHandler
from smartplier.web_service import Workspace, prepare

ROOT = Path(__file__).resolve().parents[2]


def init(profile_path):
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    for destination, source in ((profile_path, "profile"), (profile_path.with_name("discovery.json"), "discovery")):
        try:
            with destination.open("xb") as file:
                file.write((ROOT / "examples" / f"{source}.example.json").read_bytes())
        except FileExistsError:
            print(f"Kept existing {destination.name}.")
        else:
            print(f"Created {destination.name}.")
    print("Replace the fictional candidate details in your profile. Add your own discovery sources to discovery.json.")
    return 0


class DemoProvider:
    model = "fictional-demo"
    method = "gemini"

    def score(self, _payload):
        return {"fit_score": 87, "matched": ["Python", "FastAPI"], "missing": ["Production PostgreSQL"],
                "red_flags": [], "ghost_job_risk": "low", "reason": "Python services and data validation match the example role."}

    def generate_json(self, _instructions, _payload, schema):
        if "evidence_id" in schema["properties"]:
            return {"evidence_id": "lantern-api"}
        return {"summary": self.profile["basics"]["summary"], "skills": ["Python", "FastAPI", "SQL"],
                "bullets": [{"id": bullet["id"], "text": bullet["text"]}
                            for section in ("experience", "projects")
                            for entry in self.profile[section] for bullet in entry["bullets"]]}


class DemoWorkspace(Workspace):
    def gmail_configured(self):
        return False

    def queue_discovery(self, *_args, **_kwargs):
        raise ValueError("External discovery is disabled in the fictional demo.")

    def check_receipts(self, **_kwargs):
        raise ValueError("Gmail is disabled in the fictional demo.")


class DemoHandler(WebHandler):
    def post(self, path, form):
        if (path.startswith(("/api/auto-", "/api/autopilot-")) or "/auto-" in path
                or path == "/api/discover"):
            raise HttpError(403, "External integrations are disabled in the fictional demo.")
        super().post(path, form)


def seed(db_path, profile_path, provider):
    profile = provider.profile
    with database(db_path) as db:
        jobs = [
            ("Python Developer", "Hushpine Systems", "Remote", "Build FastAPI services in Python. Contact Rowan Ellif at rowan@example.com."),
            ("Data Analyst", "Velvet Comet Metrics", "Remote", "Use SQL and Python to improve dashboard data quality."),
            ("Machine Learning Engineer", "Cloudreed Instruments", "Remote", "Validate models for fictional telemetry."),
            ("Backend Developer", "Quill Harbor Systems", "Hybrid", "Maintain Python services and documentation."),
        ]
        for i, (title, company, location, description) in enumerate(jobs, 1):
            job_id = add_job(db, title, company, location, description, f"https://example.com/jobs/{i}")
            job = get_job(db, job_id)
            if i != 3:
                save_score(db, job, evaluate(job, profile, provider))
        first = get_job(db, 1)
        with db:
            outreach.store_drafts(db, first, outreach.make_drafts(first, profile, provider, "Rowan Ellif"))
            resume.generate_resume(db, db_path, profile_path, 1, provider)
            auto_apply.single_row(db, first)
            auto_apply.update(db, 1, "needs_review", "Fictional example waiting for a verified screening answer.")
            question = {"label": "Which shift do you prefer?", "type": "select-one", "options": ["Day", "Evening"]}
            ident = screening.identity(question)
            db.execute("INSERT INTO screening_questions VALUES (?,?,?,?,?,?)", (
                ident, question["label"], question["type"], json.dumps(question["options"]), "unknown", ""))
            db.execute("INSERT INTO screening_question_jobs VALUES (?,?,?)", (ident, 1, "Fictional ATS"))
            db.execute("INSERT INTO auto_preflight VALUES (?,?)", (1, json.dumps({
                "ready": False, "detail": "Fictional preview awaiting a screening answer.", "at": analytics.timestamp(),
                "fields": ["Name", "Email"], "unanswered": [question["label"]], "inspected": True,
                "controls": [{"type": question["type"], "label": question["label"], "required": True}]})))


def serve(port):
    if not (DIST / "index.html").is_file():
        raise ValueError("Build the frontend before starting the demo. Run npm --prefix frontend ci and npm --prefix frontend run build.")
    if not 0 <= port <= 65535:
        raise ValueError("Choose a port between 0 and 65535.")
    with TemporaryDirectory(prefix="smartplier-demo-") as folder:
        root = Path(folder)
        profile, db = root / "profile.json", root / "jobs.sqlite3"
        shutil.copyfile(ROOT / "examples" / "profile.example.json", profile)
        shutil.copyfile(ROOT / "examples" / "discovery.example.json", root / "discovery.json")
        provider = DemoProvider()
        provider.profile = json.loads(profile.read_text(encoding="utf-8"))
        prepare(db, profile)
        seed(db, profile, provider)
        with DashboardServer(port, db, profile, provider, DemoHandler) as server:
            server.workspace = DemoWorkspace(db, profile, provider)
            print(f"Smartplier demo is listening at {server.origin}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.workspace.close()
    return 0
