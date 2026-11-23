"""Local-only HTTP entry point for the dashboard."""

import json
import re
import secrets
import socket
import sqlite3
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from smartplier import analytics
from smartplier import dashboard_views as views
from smartplier import outreach
from smartplier.discovery import job_discovery
from smartplier.resume import resume_paths
from smartplier.scoring import MissingKey, ProviderError, evaluate, gemini_from_settings
from smartplier.tracker import STATUSES, JobNotFound, add_job, change_status, database, get_job, history, save_score, update_location


LOOPBACK = "127.0.0.1"
ASSETS = Path(__file__).resolve().parent
JOB_ROUTE = re.compile(r"/jobs/([1-9][0-9]{0,17})(?:/(status|location|score|resume|outreach|application))?")
FORM_TYPE = "application/x-www-form-urlencoded"
MAX_FORM_BYTES = 1_000_000


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port, db_path, profile_path, provider=None, handler=None):
        self.db_path = Path(db_path)
        self.profile_path = Path(profile_path)
        self.provider = provider
        self.csrf = secrets.token_urlsafe(32)
        super().__init__((LOOPBACK, port), handler or DashboardHandler)
        self.host = LOOPBACK if self.server_port == 80 else f"{LOOPBACK}:{self.server_port}"
        self.origin = f"http://{self.host}"

    def server_bind(self):
        # On Windows, SO_REUSEADDR allows two listeners on the same address.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        # Job data, query strings and credentials stay out of request logs.
        pass

    def respond(self, body=b"", status=200, media="text/html; charset=utf-8", location=None, sandbox=False, download=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        inline_preview = sandbox and getattr(self, "modern", False)
        if not inline_preview:
            self.send_header("X-Frame-Options", "DENY")
        csp = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
            if sandbox else
            "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'"
        )
        if not sandbox:
            csp += "; img-src 'self'"
            if getattr(self, "modern", False):
                csp += " data:; script-src 'self'; connect-src 'self'; style-src-attr 'unsafe-inline'; frame-src 'self'"
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.send_header("Content-Security-Policy", csp + ("; frame-ancestors 'self'" if inline_preview else "; frame-ancestors 'none'"))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            if self.headers.get_all("Host") != [self.server.host]:
                raise HttpError(403, "Open the dashboard using its printed localhost URL.")
            if not self.path.startswith("/") or self.path.startswith("//"):
                raise HttpError(400, "Invalid dashboard URL.")
            target = urlsplit(self.path)
            if self.command == "POST":
                self.post(target.path, self.read_form())
            else:
                self.get(target)
        except HttpError as error:
            self.request_error(error, error.status)
        except JobNotFound as error:
            self.request_error(error, 404)
        except (ConnectionError, TimeoutError):
            return
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, ProviderError) as error:
            self.request_failure(error)

    def request_error(self, error, status):
        self.respond(views.error(str(error)), status=status)

    def request_failure(self, error):
        self.request_error("The dashboard could not read its local data. Check the database and profile paths.", 500)

    def read_form(self):
        if self.headers.get_all("Origin") != [self.server.origin]:
            raise HttpError(403, "This form must be submitted from the dashboard.")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or self.headers.get_content_type() != FORM_TYPE:
            raise HttpError(415, "Expected a dashboard form.")
        sizes = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,9}", sizes[0]):
            raise HttpError(400, "Invalid form length.")
        size = int(sizes[0])
        if size > MAX_FORM_BYTES:
            raise HttpError(413, "The posting is too large for this form.")
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete form")
            values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, errors="strict", max_num_fields=12)
        except (ValueError, TimeoutError):
            raise HttpError(400, "Malformed form.") from None
        if any(len(items) != 1 for items in values.values()):
            raise HttpError(400, "Duplicate form fields.")
        form = {key: value[0].strip() for key, value in values.items()}
        if not secrets.compare_digest(form.pop("csrf", "").encode("utf-8"), self.server.csrf.encode("utf-8")):
            raise HttpError(403, "The form expired. Reload the dashboard and try again.")
        return form

    def get(self, target):
        if target.path == "/dashboard.css":
            self.respond((ASSETS / "dashboard.css").read_bytes(), media="text/css; charset=utf-8")
        elif target.path == "/assets/smartplier-logo.png":
            logo = ASSETS.parents[1] / "frontend" / "public" / "assets" / "smartplier-logo.png"
            self.respond(logo.read_bytes(), media="image/png")
        elif target.path == "/":
            self.index(target.query)
        elif target.path == "/jobs/new":
            self.respond(views.add_form(self.server.csrf))
        elif target.path == "/outreach/due":
            with database(self.server.db_path, self.server.profile_path) as db:
                jobs = outreach.due_jobs(db)
            self.respond(views.follow_ups(jobs))
        elif target.path == "/analytics":
            with database(self.server.db_path, self.server.profile_path) as db:
                report = analytics.response_rate(db)
            self.respond(views.analytics_page(report))
        else:
            job_id, action = self.job_route(target.path)
            if action == "resume":
                with database(self.server.db_path, self.server.profile_path) as db:
                    get_job(db, job_id)
                path = self.resume_path(job_id)
                if path is None:
                    raise HttpError(404, "A generated resume is not available for this job.")
                self.respond(path.read_bytes(), sandbox=True)
            elif action is None:
                self.detail(job_id)
            elif action == "outreach":
                self.job_outreach(job_id)
            else:
                raise HttpError(405, "Use the dashboard form for this action.")

    def post(self, path, form):
        if path == "/jobs/new":
            fields = [form.get(field, "") for field in ("title", "company", "location", "description", "url")]
            if not all(fields[index] for index in (0, 1, 3)):
                self.respond(views.add_form(self.server.csrf, form, "Enter a title, company and posting."),
                             status=400)
                return
            with database(self.server.db_path, self.server.profile_path) as db:
                job_id = add_job(db, *fields)
        else:
            job_id, action = self.job_route(path)
            if action == "status":
                if form.get("status") not in STATUSES:
                    raise HttpError(400, "Choose a valid status.")
                try:
                    with database(self.server.db_path, self.server.profile_path) as db:
                        change_status(db, job_id, form["status"], form.get("reason") or "Manual status change.",
                                      form.get("variant"))
                except analytics.AnalyticsError as error:
                    self.detail(job_id, str(error), status=400)
                    return
            elif action in ("location", "score"):
                if action == "location" and "location" not in form:
                    raise HttpError(400, "Enter the explicit location or leave it blank for review.")
                try:
                    self.score(job_id, form["location"] if action == "location" else None)
                except ProviderError as error:
                    prefix = ("The location was saved. " if action == "location" else "")
                    self.detail(job_id, prefix + str(error), status=503 if isinstance(error, MissingKey) else 502)
                    return
            elif action == "outreach":
                try:
                    self.update_outreach(job_id, form)
                except outreach.OutreachError as error:
                    self.job_outreach(job_id, str(error), status=400)
                    return
                self.respond(status=303, location=f"/jobs/{job_id}/outreach")
                return
            elif action == "application":
                try:
                    self.update_application(job_id, form)
                except analytics.AnalyticsError as error:
                    self.detail(job_id, str(error), status=400)
                    return
            else:
                raise HttpError(405, "This page does not accept form submissions.")
        self.respond(status=303, location=f"/jobs/{job_id}")

    @staticmethod
    def job_route(path):
        match = JOB_ROUTE.fullmatch(path)
        if not match:
            raise HttpError(404, "This dashboard page does not exist.")
        return int(match[1]), match[2]

    def index(self, query):
        try:
            values = parse_qs(query, max_num_fields=8)
        except ValueError:
            raise HttpError(400, "Invalid dashboard search.") from None
        tab = values.get("tab", ["to-apply"])[0]
        if tab not in views.TABS:
            raise HttpError(400, "Unknown dashboard tab.")
        search = values.get("q", [""])[0].strip()[:200]
        with database(self.server.db_path, self.server.profile_path) as db:
            counts = Counter(dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")))
            db.create_function("casefold", 1, str.casefold)
            statuses = views.TABS[tab][1]
            clause = ""
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                clause = f"AND status IN ({placeholders})"
            jobs = db.execute(
                f"""SELECT id, title, company, location, status, score, score_method FROM jobs
                    WHERE ('' = ? OR instr(casefold(title), ?) > 0 OR instr(casefold(company), ?) > 0
                        OR instr(casefold(location), ?) > 0 OR instr(casefold(description), ?) > 0)
                    {clause} ORDER BY score DESC, id DESC""",
                (search.casefold(),) * 5 + statuses,
            ).fetchall()
            due = len(outreach.due_jobs(db))
        self.respond(views.index(tab, search, jobs, counts, self.server.provider is not None, due))

    def detail(self, job_id, message="", status=200):
        with database(self.server.db_path, self.server.profile_path) as db:
            job = get_job(db, job_id)
            changes = history(db, job_id)
            source = job_discovery(db, job_id)
            application = analytics.application(db, job_id)
        self.respond(views.detail(job, changes, self.server.csrf, self.server.provider is not None,
                                  self.resume_path(job_id) is not None, message, source, application), status=status)

    def resume_path(self, job_id):
        path, _ = resume_paths(self.server.db_path, self.server.profile_path, job_id)
        if path.resolve() == path and path.is_file():
            return path
        return None

    def job_outreach(self, job_id, message="", status=200):
        with database(self.server.db_path, self.server.profile_path) as db:
            job = get_job(db, job_id)
            row = outreach.outreach_row(db, job_id)
        self.respond(views.outreach_page(job, row, self.server.csrf, message), status=status)

    def update_outreach(self, job_id, form):
        action = form.get("action")
        if action == "draft":
            with database(self.server.db_path, self.server.profile_path) as db:
                job = get_job(db, job_id)
                outreach.draftable(outreach.outreach_row(db, job_id))
            profile = json.loads(self.server.profile_path.read_text(encoding="utf-8-sig"))
            drafts = outreach.make_drafts(job, profile, self.server.provider, form.get("contact"))
            with database(self.server.db_path, self.server.profile_path) as db:
                outreach.store_drafts(db, job, drafts)
        else:
            with database(self.server.db_path, self.server.profile_path) as db:
                outreach.record_outreach(db, job_id, action, form.get("channel"))

    def update_application(self, job_id, form):
        with database(self.server.db_path, self.server.profile_path) as db, db:
            get_job(db, job_id)
            db.execute("BEGIN IMMEDIATE")
            action = form.get("action")
            if action == "reply":
                analytics.record_reply(db, job_id, analytics.timestamp())
            elif action == "variant":
                analytics.update_variant(db, job_id, form.get("variant"))
            else:
                raise analytics.AnalyticsError("Unknown application action.")

    def score(self, job_id, location):
        with database(self.server.db_path, self.server.profile_path) as db:
            job = get_job(db, job_id) if location is None else update_location(db, job_id, location)
            counts = analytics.feedback_counts(db) if self.server.provider else {}
        profile = json.loads(self.server.profile_path.read_text(encoding="utf-8-sig"))
        evaluation = evaluate(job, profile, self.server.provider, analytics.feedback_for(counts, job["title"]))
        with database(self.server.db_path, self.server.profile_path) as db:
            try:
                save_score(db, job, evaluation)
            except ValueError as error:
                raise HttpError(409, str(error)) from None


def serve(db_path, profile_path, port):
    if not 1 <= port <= 65535:
        raise ValueError("The dashboard port must be between 1 and 65535.")
    with DashboardServer(port, db_path, profile_path, gemini_from_settings()) as server:
        print(f"Smartplier is listening at {server.origin}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
