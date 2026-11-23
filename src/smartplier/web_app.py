"""Local JSON API and production React assets."""
import json
import mimetypes
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

from smartplier import analytics
from smartplier import auto_apply
from smartplier.ats_detection import detect
from smartplier import outreach
from smartplier import screening
from smartplier.dashboard import DashboardHandler, DashboardServer, HttpError
from smartplier.dashboard_views import TABS
from smartplier.discovery import job_discovery
from smartplier.resume import resume_paths
from smartplier.scoring import ProviderError, gemini_from_settings
from smartplier.tracker import STATUSES, add_job, change_status, get_job, history, update_location
from smartplier.web_service import Workspace, prepare

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
PAGES = {"/", "/discover", "/jobs", "/resumes", "/outreach", "/analytics", "/settings", "/auto-apply"}
SORT = {"fit": "score DESC,id DESC", "recent": "id DESC", "oldest": "id ASC", "title": "title COLLATE NOCASE,id DESC"}

class WebHandler(DashboardHandler):
    modern = True

    def reply(self, data, status=200):
        self.respond(json.dumps(data, default=dict), status, "application/json; charset=utf-8")

    def request_error(self, error, status):
        self.reply({"error": str(error)}, status)

    def request_failure(self, error):
        if isinstance(error, (OSError, sqlite3.Error)):
            self.request_error("The local database or file could not be accessed.", 500)
        else:
            self.request_error(error if isinstance(error, (ValueError, ProviderError)) else "Invalid request fields.", 400)

    @property
    def work(self):
        return self.server.workspace

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        previous = ""
        for _ in range(45):
            data = json.dumps(self.work.snapshot())
            message = f"data: {data}\n\n" if data != previous else ": heartbeat\n\n"
            self.wfile.write(message.encode())
            self.wfile.flush()
            previous = data
            if self.work.stop.wait(1):
                break

    def get(self, target):
        path = target.path
        query = {k: v[0] for k, v in parse_qs(target.query, max_num_fields=16).items()}
        if path in PAGES:
            self.respond((DIST / "index.html").read_bytes())
        elif re.fullmatch(r"/assets/[a-zA-Z0-9_.-]+", path):
            file = DIST / path.lstrip("/")
            if not file.is_file():
                raise HttpError(404, "Asset not found.")
            media = {".js": "text/javascript", ".css": "text/css"}.get(file.suffix)
            self.respond(file.read_bytes(), media=media or mimetypes.guess_type(file)[0] or "application/octet-stream")
        elif path == "/api/events":
            self.events()
        elif path == "/api/settings":
            data = self.work.discovery_view()
            prefs = self.work.profile_data()["preferences"]
            data["preferences"] = {key: prefs[key] for key in
                                   ("min_score_to_apply", "open_to_remote", "target_roles", "deal_breakers", "avoid_companies")}
            self.reply(data)
        elif path == "/api/sync-history":
            self.reply(self.work.history(query.get("source")))
        elif path == "/api/auto-apply":
            with self.work.connection() as db:
                self.reply(auto_apply.report(db))
        elif path == "/api/screening":
            with self.work.connection() as db:
                self.reply({"questions": screening.inbox(db),
                            "drafts_enabled": auto_apply.settings(db)["screening_drafts"]})
        elif evidence := re.fullmatch(r"/api/jobs/([1-9][0-9]{0,17})/diagnostic/([1-9][0-9]{0,17})/(html|png)", path):
            job, ident, kind = evidence.groups()
            column = "page_html" if kind == "html" else "screenshot"
            with self.work.connection() as db:
                found = db.execute(f"SELECT {column} FROM auto_diagnostics WHERE id=? AND job_id=?", (ident, job)).fetchone()
            if not found or found[0] is None:
                raise HttpError(404, "Diagnostic evidence was not captured.")
            body = found[0].encode("utf-8") if kind == "html" else found[0]
            self.respond(body, media="text/plain; charset=utf-8" if kind == "html" else "image/png",
                         download=f"application-{ident}." + ("html.txt" if kind == "html" else "png"))
        elif path == "/api/jobs":
            self.jobs(query)
        elif path in {"/api/overview", "/api/analytics", "/api/outreach", "/api/resumes"}:
            with self.work.connection() as db:
                if path == "/api/analytics":
                    result = analytics.response_rate(db)
                elif path == "/api/outreach":
                    result = {"due": outreach.due_jobs(db), "drafts": db.execute(
                        "SELECT j.id,j.title,j.company,o.sent_at,o.replied_at FROM outreach o JOIN jobs j ON j.id=o.job_id ORDER BY o.created_at DESC").fetchall()}
                elif path == "/api/resumes":
                    result = db.execute("SELECT j.id,j.title,j.company FROM resume_cache r JOIN jobs j ON j.id=r.job_id ORDER BY j.id DESC").fetchall()
                else:
                    result = {"csrf": self.server.csrf, "gemini": bool(self.work.provider), "counts": dict(db.execute(
                        "SELECT status,COUNT(*) FROM jobs GROUP BY status")), "due": len(outreach.due_jobs(db)),
                        "analytics": analytics.response_rate(db), "tasks": self.work.snapshot(),
                        "recent": db.execute("SELECT id,title,company,status,score FROM jobs ORDER BY id DESC LIMIT 6").fetchall()}
            self.reply(result)
        else:
            ident, action = self.job_path(path)
            if action in {"html", "tex", "preview"}:
                self.file(ident, action)
            elif action is None:
                self.detail(ident)
            else:
                raise HttpError(405, "Use the dashboard action.")

    def job_path(self, path):
        match = re.fullmatch(r"/api/jobs/([1-9][0-9]{0,17})(?:/([a-z-]+))?", path)
        if not match:
            raise HttpError(404, "Page not found.")
        return int(match[1]), match[2]

    def jobs(self, query):
        tab = query.get("tab", "all")
        if tab not in TABS:
            raise ValueError("Unknown job status tab.")
        sort = SORT.get(query.get("sort"), SORT["fit"])
        page = max(1, min(int(query.get("page", 1)), 100000))
        term = query.get("q", "")[:200].casefold()
        statuses = TABS[tab][1]
        where = "WHERE (? = '' OR instr(casefold(title||' '||company||' '||location||' '||description),?) > 0)"
        if statuses:
            where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        params = (term, term, *statuses)
        with self.work.connection() as db:
            db.create_function("casefold", 1, str.casefold)
            total = db.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT id,title,company,location,score,score_method,status FROM jobs {where} ORDER BY {sort} LIMIT 25 OFFSET ?", (*params,(page-1)*25)).fetchall()
            counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        self.reply({"rows": rows, "total": total, "page": page, "counts": counts})

    def detail(self, ident):
        with self.work.connection() as db:
            job = dict(get_job(db, ident))
            data = {"job": job, "history": history(db, ident), "source": job_discovery(db, ident),
                    "application": analytics.application(db, ident), "outreach": outreach.outreach_row(db, ident),
                    "people": outreach.contact_names(job["description"]), "links": outreach.search_links(job),
                    "preflight": auto_apply.recorded_preflight(db, ident),
                    "ats": detect(job["url"]).as_dict(),
                    "freshness_override": auto_apply.freshness_override(db, ident),
                    "auto_enabled": auto_apply.settings(db)["enabled"]}
            attempt = db.execute("SELECT snapshot,confirmation_state,confirmation_url FROM auto_attempt_log WHERE job_id=?", (ident,)).fetchone()
            data["attempt"] = ({**dict(attempt), "snapshot": json.loads(attempt["snapshot"])} if attempt else None)
            data["diagnostics"] = [dict(row) for row in db.execute(
                """SELECT id,at,phase,page_html IS NOT NULL AS html,screenshot IS NOT NULL AS png
                   FROM auto_diagnostics WHERE job_id=? ORDER BY id DESC LIMIT 20""", (ident,))]
        for key in ("contacts", "score_details"):
            job[key] = json.loads(job[key]) if job[key] else None
        row = data["outreach"]
        if row:
            data["outreach"] = dict(row)
            data["outreach"]["drafts"] = json.loads(row["drafts"])
            if row["sent_at"]:
                data["outreach"]["due"] = outreach.due_at(row["sent_at"]).isoformat()
            try:
                data["outreach"]["followup"] = outreach.follow_up(job, row)
            except outreach.OutreachError:
                pass
        data["resume"] = [p.is_file() for p in resume_paths(self.work.db, self.work.profile, ident)]
        self.reply(data)

    def file(self, ident, action):
        with self.work.connection() as db:
            get_job(db, ident)
        paths = resume_paths(self.work.db, self.work.profile, ident)
        path = paths[action == "tex"]
        if path.resolve() != path or not path.is_file():
            raise HttpError(404, "Generate a resume first.")
        preview = action == "preview"
        self.respond(path.read_bytes(), media="text/html; charset=utf-8" if preview else "application/octet-stream",
                     sandbox=preview, download=None if preview else path.name)

    def post(self, path, form):
        data = json.loads(form.get("payload", "{}"))
        if not isinstance(data, dict):
            raise ValueError("Expected an object.")
        if path == "/api/settings":
            self.work.save_settings(data)
        elif path == "/api/preferences":
            self.work.save_preferences(data)
        elif path == "/api/discover":
            return self.reply({"task": self.work.queue_discovery(data.get("source"))}, 202)
        elif path == "/api/score":
            return self.reply({"task": self.work.submit_score()}, 202)
        elif path == "/api/auto-apply":
            return self.reply({"task": auto_apply.queue(self.work)}, 202)
        elif path == "/api/auto-receipts":
            return self.reply({"task": self.work.check_receipts(requested=True)}, 202)
        elif path in {"/api/autopilot-resume", "/api/autopilot-ramp"}:
            with self.work.connection() as db:
                if path == "/api/autopilot-resume":
                    auto_apply.resume_autopilot(db)
                else:
                    return self.reply({"daily_cap": auto_apply.ramp_daily_cap(db)})
        elif path == "/api/auto-apply-settings":
            with self.work.connection() as db:
                auto_apply.save_settings(db, data)
        elif match := re.fullmatch(r"/api/screening/([a-f0-9]{64})(/draft)?", path):
            ident, draft = match.groups()
            if draft:
                with self.work.connection() as db:
                    if not auto_apply.settings(db)["screening_drafts"]:
                        raise ValueError("Enable optional screening drafts in Auto-Apply settings first.")
                    question = screening.get_question(db, ident)
                value = screening.draft_answer(self.work.provider, question, self.work.profile_data())
                with self.work.connection() as db, db:
                    db.execute("""INSERT INTO auto_events(job_id,at,status,detail)
                                  SELECT job_id,?,'generated','Generated a local screening draft for review.'
                                  FROM screening_question_jobs WHERE question_id=?""", (analytics.timestamp(), ident))
                return self.reply({"draft": value})
            with self.work.connection() as db:
                jobs = screening.save(db, ident, data)
            try:
                task = self.work.queue("screening:" + ident, "Recheck screening answers",
                                       [(f"Job #{job}", job) for job in jobs],
                                       lambda job, _run: auto_apply.recheck_screening(self.work, job)) if jobs else None
            except ValueError:
                return self.reply({"saved": True, "jobs": jobs, "retry": True})
            return self.reply({"saved": True, "jobs": jobs, "task": task})
        elif path == "/api/jobs":
            if not all(data.get(k, "").strip() for k in ("title", "company", "description")):
                raise ValueError("Title, company, and posting are required.")
            with self.work.connection() as db:
                ident = add_job(db, *(data.get(k, "").strip() for k in ("title","company","location","description","url")))
            return self.reply({"id": ident}, 201)
        else:
            ident, action = self.job_path(path)
            if action == "score":
                return self.reply({"task": self.work.submit_score(ident)}, 202)
            if action == "resume":
                return self.reply({"task": self.work.resume(ident, data.get("force") is True)}, 202)
            if action in {"auto-preflight", "auto-submit"}:
                return self.reply({"task": auto_apply.queue_single(self.work, ident, submit=action == "auto-submit")}, 202)
            if action == "auto-assisted":
                return self.reply({"task": auto_apply.queue_assisted(self.work, ident)}, 202)
            if action == "auto-cancel":
                auto_apply.cancel_scheduled(self.work, ident)
                return self.reply({"ok": True})
            if action == "auto-manual-done":
                auto_apply.manual_done(self.work, ident)
                return self.reply({"ok": True})
            if action == "freshness-override":
                with self.work.connection() as db:
                    auto_apply.save_override(db, ident, data.get("reason", ""))
                return self.reply({"ok": True})
            if action == "draft":
                def draft(_item, _run):
                    with self.work.connection() as db:
                        job = get_job(db, ident)
                        outreach.draftable(outreach.outreach_row(db, ident))
                    drafts = outreach.make_drafts(job, self.work.profile_data(), self.work.provider, data.get("contact"))
                    with self.work.connection() as db:
                        outreach.store_drafts(db, job, drafts)
                    return {"warning": drafts["warning"]}
                return self.reply({"task": self.work.queue(f"draft:{ident}", "Draft outreach", [(f"Job #{ident}", ident)], draft)}, 202)
            with self.work.connection() as db:
                if action == "status":
                    change_status(db, ident, data["status"], data.get("reason") or "Dashboard status update.", data.get("variant"))
                elif action == "location":
                    update_location(db, ident, data["location"].strip())
                elif action == "outreach":
                    outreach.record_outreach(db, ident, data["action"], data.get("channel"))
                elif action == "application":
                    get_job(db, ident)
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        if data["action"] == "reply":
                            analytics.record_reply(db, ident, analytics.timestamp())
                        elif data["action"] == "variant":
                            analytics.update_variant(db, ident, data["variant"])
                        else:
                            raise ValueError("Unknown application action.")
                elif action == "edit-draft":
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        row = outreach.outreach_row(db, ident)
                        if row is None:
                            raise ValueError("Generate a draft first.")
                        draft = json.loads(row["drafts"])
                        if "followup" in data:
                            outreach.check_follow_up(get_job(db, ident), row)
                            if not isinstance(data["followup"], str) or not data["followup"].strip() or len(data["followup"]) > 4000:
                                raise ValueError("Enter follow-up text of at most 4000 characters.")
                            draft["followup_override"] = data["followup"]
                        else:
                            outreach.draftable(row)
                            if not all(isinstance(data.get(k), str) for k in ("subject","email","note")) or len(data["note"]) > outreach.NOTE_LIMIT:
                                raise ValueError("Enter the draft text and keep the LinkedIn note at 200 characters or fewer.")
                            draft.update({k: data[k] for k in ("subject","email","note")})
                        db.execute("UPDATE outreach SET drafts=? WHERE job_id=?", (json.dumps(draft), ident))
                else:
                    raise HttpError(404, "Action not found.")
        self.reply({"ok": True})

def serve_web(db_path, profile_path, port):
    if not (DIST / "index.html").is_file():
        raise ValueError("Production assets are missing. Run npm --prefix frontend ci and npm --prefix frontend run build.")
    if not 1 <= port <= 65535:
        raise ValueError("Use a port between 1 and 65535.")
    with DashboardServer(port, db_path, profile_path, gemini_from_settings(), handler=WebHandler) as server:
        prepare(db_path, profile_path)
        server.workspace = Workspace(db_path, profile_path, server.provider)
        print(f"Smartplier web is listening at {server.origin}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.workspace.close()
    return 0
