"""Command-line entry point for Smartplier."""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from smartplier import analytics
from smartplier.credentials import save_gemini_key, save_gmail_credentials
from smartplier.discovery import discover, job_discovery
from smartplier import outreach
from smartplier.scoring import ProviderError, evaluate, gemini_from_settings
from smartplier.tracker import STATUSES, add_job, change_status, database, get_job, history, save_score, scoring_queue


ROOT = Path(__file__).resolve().parents[2]
TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def display(text):
    print(TERMINAL_CONTROLS.sub("", text))


def job_line(job):
    result = f'{job["score"] if job["score"] is not None else "-"} [{job["score_method"] or "-"}]'
    return f'#{job["id"]} {job["status"]} {result} | {job["title"]} | {job["company"]} | {job["location"] or "(none)"}'


def show_job(db, job_id):
    job = get_job(db, job_id)
    display(job_line(job))
    display(f'Added: {job["added_at"]}\nURL: {job["url"] or "-"}')
    source = job_discovery(db, job_id)
    if source:
        display(f'Source: {source["source"]} | First seen: {source["first_seen"]} | Last seen: {source["last_seen"]}')
        if source["published"]:
            display(f'Published: {source["published"]}')
        for reason in source["flags"]:
            display(f"Posting signal: {reason}")
    application = analytics.application(db, job_id)
    if application:
        display(f'Application resume variant: {application["variant"]}')
        display(f'Application response: {application["response_at"] or "(pending)"}')
    contacts = json.loads(job["contacts"])
    display("Emails: " + (", ".join(contacts["emails"]) or "-"))
    display("LinkedIn profiles: " + (", ".join(contacts["linkedin_profiles"]) or "-"))
    if job["score_details"] is not None:
        result = json.loads(job["score_details"])
        display(f'Scored: {job["scored_at"]}')
        display(f'Reason: {result["reason"]}\nGhost-job risk: {result["ghost_job_risk"]}')
        for key, label in (("matched", "Matched"), ("missing", "Missing"), ("red_flags", "Red flags")):
            display(f'{label}: {", ".join(result[key]) or "-"}')
    display(f'\nPosting\n{job["description"]}')
    display("\nStatus history")
    for event in history(db, job_id):
        display(f'{event["changed_at"]} {event["previous_status"] or "-"} -> {event["status"]} | {event["reason"]}')


def run_scores(db, args, provider):
    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    jobs = [get_job(db, args.id)] if args.id is not None else scoring_queue(db)
    counts = analytics.feedback_counts(db) if provider and jobs else {}
    failures = 0
    if not jobs:
        print("No jobs need scoring.")
    for job in jobs:
        try:
            evaluation = evaluate(job, profile, provider, analytics.feedback_for(counts, job["title"]))
            status = save_score(db, job, evaluation)
        except (ProviderError, ValueError, sqlite3.Error) as error:
            print(f'Job #{job["id"]}: {error}', file=sys.stderr)
            failures += 1
            continue
        score = evaluation.details["fit_score"]
        result = f"{score}/100" if score is not None else "-"
        display(f'#{job["id"]} {status} {result} [{evaluation.method}] | {evaluation.details["reason"]}')
    return int(bool(failures))


def run_analytics(db, args):
    if args.reply is not None:
        get_job(db, args.reply)
        with db:
            changed = analytics.record_reply(db, args.reply, analytics.timestamp())
        print(f"Job #{args.reply}: application reply {'recorded' if changed else 'already recorded'}.")
        return
    if args.variant:
        raw_id, variant = args.variant
        job_id = int(raw_id)
        get_job(db, job_id)
        with db:
            db.execute("BEGIN IMMEDIATE")
            changed = analytics.update_variant(db, job_id, variant)
        print(f"Job #{job_id}: resume variant {'updated' if changed else 'unchanged'}.")
        return
    report = analytics.response_rate(db)
    if not report["applications"]:
        print("No recorded applications yet.")
        return
    applications, responses = report["applications"], report["responses"]
    print(f"Application response rate: {responses}/{applications} ({responses / applications:.1%})")
    for key, label in analytics.DIMENSIONS:
        print(f"\n{label}")
        for name, total, replies in report["groups"][key]:
            display(f"  {name} | {replies}/{total} responses | {replies / total:.1%}")


def print_drafts(drafts):
    display(f'Contact: {drafts["contact"] or "(add a name after checking the posting or search results)"}')
    if drafts["warning"]:
        display(f'Note: {drafts["warning"]}')
    display(f'\nEmail subject: {drafts["subject"]}\n\n{drafts["email"]}\n\nLinkedIn connection note\n{drafts["note"]}')


def show_outreach(db, job):
    people = outreach.contact_names(job["description"])
    contacts = json.loads(job["contacts"])
    display("Named in posting: " + (", ".join(people) or "-"))
    display("Posting emails: " + (", ".join(contacts["emails"]) or "-"))
    display("Posting LinkedIn profiles: " + (", ".join(contacts["linkedin_profiles"]) or "-"))
    for label, url in outreach.search_links(job):
        display(f"{label}: {url}")
    row = outreach.outreach_row(db, job["id"])
    if row:
        print_drafts(json.loads(row["drafts"]))
        if row["sent_at"]:
            until = outreach.due_at(row["sent_at"]).astimezone().isoformat(timespec="minutes")
            display(f'\nInitial message recorded via {row["channel"]} at {row["sent_at"]}.')
            if row["replied_at"]:
                print(f'Reply recorded at {row["replied_at"]}.')
            elif row["followed_up_at"]:
                print(f'Follow-up logged at {row["followed_up_at"]}.')
            elif job["status"] not in outreach.STOP_STATUSES:
                print(f"Follow-up due at {until}.")


def run_outreach(db, args):
    job = get_job(db, args.id)
    action = args.outreach_action
    if action == "draft":
        outreach.draftable(outreach.outreach_row(db, job["id"]))
        profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
        drafts = outreach.make_drafts(job, profile, gemini_from_settings(), args.contact)
        outreach.store_drafts(db, job, drafts)
        show_outreach(db, job)
    elif action == "show":
        show_outreach(db, job)
    elif action == "follow-up":
        subject, body = outreach.follow_up(job, outreach.outreach_row(db, job["id"]))
        display(f"Follow-up subject: {subject}\n\n{body}")
    else:
        changed = outreach.record_outreach(db, job["id"], action, getattr(args, "via", None))
        print(f"Job #{job['id']}: {action} {'recorded' if changed else 'already recorded'}.")


def run_due(args):
    if args.interval_minutes is not None and args.interval_minutes < 1:
        raise ValueError("The follow-up interval must be at least one minute.")
    reported = set()
    try:
        while True:
            with database(args.db, args.profile) as db:
                jobs = outreach.due_jobs(db)
            for job in jobs:
                if job["id"] not in reported:
                    due = outreach.due_at(job["sent_at"]).astimezone().date().isoformat()
                    display(f'#{job["id"]} Follow-up due {due} | {job["title"]} | {job["company"]}')
            if args.interval_minutes is None:
                if not jobs:
                    print("No follow-ups due.")
                return 0
            reported = {job["id"] for job in jobs}
            sys.stdout.flush()
            time.sleep(args.interval_minutes * 60)
    except KeyboardInterrupt:
        return 0


def parser():
    cli = argparse.ArgumentParser(prog="Smartplier", description="Local job application tracker")
    cli.add_argument("--db", type=Path, default=ROOT / "jobs.sqlite3", help="SQLite database file")
    cli.add_argument("--profile", type=Path, default=ROOT / "profile.json", help="Candidate profile file")
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create local configuration from fictional examples")
    demo = sub.add_parser("demo", help="Start a temporary dashboard with fictional data")
    demo.add_argument("--port", type=int, default=8011)
    sub.add_parser("launch", help="Start the dashboard and open the default browser")

    add = sub.add_parser("add", help="Record a pasted posting")
    add.add_argument("--title", required=True)
    add.add_argument("--company", required=True)
    add.add_argument("--location", default="", help="Explicit location from the posting")
    add.add_argument("--url", default="")
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--description", help="Pasted posting text")
    source.add_argument("--file", help="UTF-8 posting file, or - for UTF-8 stdin")

    score = sub.add_parser("score", help="Check eligibility and score eligible postings with Gemini")
    score.add_argument("--id", type=int, help="Re-score one posting")

    resume = sub.add_parser("resume", help="Generate or open the cached HTML and LaTeX for a job")
    resume.add_argument("id", type=int)
    resume.add_argument("--force", action="store_true", help="Request a fresh Gemini selection")

    listing = sub.add_parser("list", help="List tracked postings")
    listing.add_argument("--status", choices=STATUSES)

    show = sub.add_parser("show", help="Show a posting and its history")
    show.add_argument("id", type=int)

    status = sub.add_parser("status", help="Set a job's status")
    status.add_argument("id", type=int)
    status.add_argument("status", choices=STATUSES)
    status.add_argument("--reason", default="Manual status change.")
    status.add_argument("--variant", help="Resume variant used when recording an application")

    stats = sub.add_parser("analytics", help="Report application response rates")
    stats_actions = stats.add_mutually_exclusive_group()
    stats_actions.add_argument("--reply", type=int, metavar="ID", help="Record a reply about a submitted application")
    stats_actions.add_argument("--variant", nargs=2, metavar=("ID", "LABEL"), help="Correct the recorded resume variant")

    prefill = sub.add_parser("prefill", help="Review basic prefilled values on a supported Lever form")
    prefill.add_argument("id", type=int)

    serve = sub.add_parser("serve", help="Run the dashboard on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    web = sub.add_parser("web", help="Run the interactive dashboard on loopback")
    web.add_argument("--port", type=int, default=8010)
    discovery = sub.add_parser("discover", help="Import and pre-filter jobs from configured sources")
    discovery.add_argument("--config", type=Path, default=ROOT / "discovery.json")
    discovery.add_argument("--source", choices=("ats", "rss", "adzuna", "gmail"), help="Run only one source type")
    discovery.add_argument("--interval-minutes", type=int, help="Repeat while this process is running")
    sub.add_parser("setup-gemini", help="Save a Gemini key in Windows Credential Manager")
    sub.add_parser("setup-gmail", help="Save Gmail IMAP credentials in Windows Credential Manager")
    outreach_cmd = sub.add_parser("outreach", help="Draft messages and track manual outreach")
    actions = outreach_cmd.add_subparsers(dest="outreach_action", required=True)
    draft = actions.add_parser("draft", help="Draft an email and LinkedIn note")
    draft.add_argument("id", type=int)
    draft.add_argument("--contact", help="Name of the person you chose")
    for action, help_text in (
        ("show", "Show contacts and saved drafts"),
        ("sent", "Record an initial message you sent"),
        ("replied", "Record a reply you received"),
        ("follow-up", "Show a draft follow-up"),
        ("followed-up", "Record a follow-up you sent"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("id", type=int)
        if action == "sent":
            command.add_argument("--via", choices=outreach.CHANNELS, required=True)
    due = actions.add_parser("due", help="Show follow-ups that are due")
    due.add_argument("--interval-minutes", type=int, help="Keep checking for new reminders until stopped")
    return cli


def read_posting(args):
    if args.description is not None:
        return args.description
    if args.file == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig")
    return Path(args.file).read_text(encoding="utf-8-sig")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            from smartplier.demo import init
            return init(args.profile)
        if args.command == "demo":
            from smartplier.demo import serve
            return serve(args.port)
        if args.command == "launch":
            from smartplier.launch import main as launch
            return launch()
        if args.command == "setup-gemini":
            save_gemini_key()
            print("Gemini key saved in Windows Credential Manager.")
            return 0
        if args.command == "setup-gmail":
            save_gmail_credentials()
            print("Gmail credentials saved in Windows Credential Manager.")
            return 0
        if args.command == "discover":
            return discover(args)
        if args.command == "outreach" and args.outreach_action == "due":
            return run_due(args)
        if args.command == "prefill":
            from smartplier.prefill import review_prefill

            with database(args.db, args.profile) as db:
                url = get_job(db, args.id)["url"]
            profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
            review_prefill(url, profile)
            return 0
        if args.command == "serve":
            from smartplier.dashboard import serve

            return serve(args.db, args.profile, args.port)
        if args.command == "web":
            from smartplier.web_app import serve_web

            return serve_web(args.db, args.profile, args.port)
        with database(args.db, args.profile) as db:
            if args.command == "outreach":
                run_outreach(db, args)
            elif args.command == "analytics":
                run_analytics(db, args)
            elif args.command == "add":
                title, company, description = (value.strip() for value in
                                               (args.title, args.company, read_posting(args)))
                if not (title and company and description):
                    raise ValueError("Title, company and description are required.")
                job_id = add_job(db, title, company, args.location.strip(), description, args.url.strip())
                print(f"Added job #{job_id} (unscored).")
            elif args.command == "score":
                return run_scores(db, args, gemini_from_settings())
            elif args.command == "resume":
                from smartplier.resume import generate_resume

                paths, warnings, cached = generate_resume(db, args.db, args.profile, args.id,
                                                          gemini_from_settings(), args.force)
                print(f'{"Cached" if cached else "Generated"} resume for job #{args.id}.')
                for path in paths:
                    display(str(path))
                for warning in warnings:
                    display(f"Warning: {warning}")
            elif args.command == "list":
                if args.status:
                    jobs = db.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (args.status,))
                else:
                    jobs = db.execute("SELECT * FROM jobs ORDER BY id")
                for job in jobs:
                    display(job_line(job))
            elif args.command == "show":
                show_job(db, args.id)
            elif args.command == "status":
                changed = change_status(db, args.id, args.status, args.reason.strip() or "Manual status change.",
                                        args.variant)
                print(f'Job #{args.id} {"updated to" if changed else "already has status"} {args.status}.')
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, ProviderError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
