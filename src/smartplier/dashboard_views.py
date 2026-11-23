"""Server-rendered views. All values from postings and models are escaped."""

import json
from html import escape
from urllib.parse import urlencode, urlsplit

from smartplier import analytics
from smartplier import outreach
from smartplier.tracker import STATUSES


TABS = {
    "to-apply": ("To apply", ("to_apply",)),
    "review": ("Needs review", ("review",)),
    "unscored": ("Unscored", ("unscored",)),
    "in-progress": ("In progress", ("applied", "interview", "offer")),
    "low-filtered": ("Low-filtered", ("low", "filtered")),
    "closed": ("Closed", ("rejected", "withdrawn", "closed")),
    "all": ("All", ()),
}
STATUS_LABELS = {
    "unscored": "Unscored", "review": "Needs review", "to_apply": "To apply",
    "low": "Low fit", "filtered": "Filtered", "applied": "Applied",
    "interview": "Interview", "offer": "Offer", "rejected": "Rejected",
    "withdrawn": "Withdrawn", "closed": "Closed",
}
METHOD_LABELS = {"gemini": "Gemini", "filter": "Filter"}


def page(title, content):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · Smartplier</title>
  <link rel="icon" type="image/png" href="/assets/smartplier-logo.png">
  <link rel="stylesheet" href="/dashboard.css">
</head>
<body>
<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="/"><img src="/assets/smartplier-logo.png" alt="Smartplier" width="1536" height="1024"></a>
    <nav aria-label="Main navigation">
      <a href="/">Job tracker</a>
      <a href="/outreach/due">Follow-ups</a>
      <a href="/analytics">Analytics</a>
      <a class="button primary compact" href="/jobs/new">Add job</a>
    </nav>
  </div>
</header>
<main class="wrap">{content}</main>
</body>
</html>"""


def tab_href(tab, search=""):
    return "/?" + escape(urlencode({"tab": tab, **({"q": search} if search else {})}))


def badge(value, labels):
    return f'<span class="badge {escape(value)}">{escape(labels.get(value, value))}</span>'


def fit_cell(job):
    score = job["score"]
    number = f'<strong>{int(score)}</strong><span class="out-of">/100</span>' if score is not None else "—"
    method = job["score_method"]
    return number + (badge(method, METHOD_LABELS) if method else "")


def index(tab, search, jobs, counts, gemini, due):
    total = sum(counts.values())
    tabs = []
    for key, (label, statuses) in TABS.items():
        count = sum(counts[s] for s in statuses) if statuses else total
        current = ' aria-current="page"' if key == tab else ""
        tabs.append(f'<a href="{tab_href(key, search)}"{current}>{label}<span>{count}</span></a>')
    rows = []
    for job in jobs:
        rows.append(f"""<tr>
<td><a class="job-title" href="/jobs/{int(job['id'])}">{escape(job['title'])}</a>
<div class="muted">{escape(job['company'])}</div></td>
<td>{escape(job['location'] or 'Unspecified')}</td>
<td>{badge(job['status'], STATUS_LABELS)}</td>
<td class="fit">{fit_cell(job)}</td></tr>""")
    if rows:
        results = ("""<div class="table-scroll"><table><thead><tr><th scope="col">Role</th>
                   <th scope="col">Location</th><th scope="col">Status</th><th scope="col">Fit</th>
                   </tr></thead><tbody>""" + "".join(rows) + "</tbody></table></div>")
    elif search:
        results = '<div class="empty">No jobs match this search.</div>'
    elif total:
        results = '<div class="empty">No jobs in this tab.</div>'
    else:
        results = '<div class="empty">No jobs yet. <a href="/jobs/new">Add a posting</a> to get started.</div>'
    reminder = ""
    if due:
        reminder = (f'<p class="notice"><a href="/outreach/due">{due} '
                    f'{"follow-up" if due == 1 else "follow-ups"} due</a></p>')
    return page("Job tracker", f"""
<div class="page-heading">
  <div><h1>Job tracker</h1><p class="muted">{total} {'job' if total == 1 else 'jobs'} tracked</p></div>
  {badge("gemini" if gemini else "unscored", {"gemini": "Gemini configured", "unscored": "Gemini key needed"})}
</div>
{reminder}
<nav class="tabs" aria-label="Job status">{''.join(tabs)}</nav>
<section class="panel" aria-labelledby="results">
  <div class="panel-heading">
    <h2 id="results">{TABS[tab][0]} <span class="muted result-count">({len(jobs)})</span></h2>
    <form class="search" action="/" method="get" role="search">
      <input type="hidden" name="tab" value="{escape(tab)}">
      <input name="q" type="search" aria-label="Search jobs" value="{escape(search)}"
             maxlength="200" placeholder="Search jobs">
      <button class="button" type="submit">Search</button>
      {f'<a href="{tab_href(tab)}">Clear</a>' if search else ""}
    </form>
  </div>
  {results}
</section>""")


def csrf_field(csrf):
    return f'<input type="hidden" name="csrf" value="{escape(csrf)}">'


def feedback(message):
    return f'<p class="notice" role="alert">{escape(message)}</p>' if message else ""


def add_form(csrf, fields=None, message=""):
    fields = fields or {}
    title, company, location, description, url = (escape(fields.get(field, "")) for field in
                                                   ("title", "company", "location", "description", "url"))
    return page("Add job", f"""
<div class="page-heading"><h1>Add job</h1><a href="/?tab=all">All jobs</a></div>
{feedback(message)}
<form action="/jobs/new" method="post" class="panel block form">
  {csrf_field(csrf)}
  <div class="form-grid">
    <label>Job title<input name="title" value="{title}" required></label>
    <label>Company<input name="company" value="{company}" required></label>
    <label>Explicit location<input name="location" value="{location}" placeholder="e.g. Hyderabad, India"></label>
    <label>Posting URL <span class="muted">(optional)</span><input name="url" value="{url}" inputmode="url"></label>
  </div>
  <label>Posting<textarea name="description" rows="12" required>{description}</textarea></label>
  <button type="submit" class="button primary">Add job</button>
</form>""")


def web_link(url, label):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password and "\\" not in url
                 and all(ord(char) > 32 for char in url))
    except ValueError:
        valid = False
    if valid:
        return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>'
    return escape(label)


def item_list(values):
    return "<ul>" + "".join(f"<li>{escape(value)}</li>" for value in values) + "</ul>" if values else "<p>—</p>"


def score_details(job):
    if job["score_details"] is None:
        return '<p class="muted">This job has no score yet.</p>'
    details = json.loads(job["score_details"])
    risk = escape(details["ghost_job_risk"])
    evidence = "".join(
        f"<section><h3>{label}</h3>{item_list(details[key])}</section>"
        for key, label in (("matched", "Matched"), ("missing", "Missing"), ("red_flags", "Red flags"))
    )
    return (f'<p>{escape(details["reason"])}</p><dl class="metrics"><dt>Ghost-job risk</dt><dd>{risk}</dd></dl>'
            f'<div class="evidence">{evidence}</div>')


def detail(job, changes, csrf, gemini, has_resume, message, source, application):
    job_id = int(job["id"])
    token = csrf_field(csrf)
    status_options = []
    for status in STATUSES:
        selected = " selected" if status == job["status"] else ""
        status_options.append(f'<option value="{status}"{selected}>{STATUS_LABELS[status]}</option>')
    contacts = json.loads(job["contacts"])
    profiles = "".join(f"<li>{web_link(url, url)}</li>" for url in contacts["linkedin_profiles"])
    changes_html = []
    for row in reversed(changes):
        before = STATUS_LABELS.get(row["previous_status"], "Added")
        after = STATUS_LABELS.get(row["status"], row["status"])
        transition = f"{before} → {after}" if before != after else after
        changes_html.append(f"""<li><div><strong>{escape(transition)}</strong>
          <time datetime="{escape(row['changed_at'])}">{escape(row['changed_at'])}</time></div>
          <div>{escape(row['reason'])}</div></li>""")
    score_label = "Score with Gemini" if gemini else "Check eligibility"
    resume = (f'<a class="button" href="/jobs/{job_id}/resume" target="_blank" rel="noopener noreferrer">'
              'Open generated resume</a>' if has_resume else '<p class="muted">No generated resume available.</p>')
    attribution = ""
    if source:
        label = escape(source["source"])
        if job["url"]:
            label = web_link(job["url"], source["source"])
        published = f' · Published {escape(source["published"])}' if source["published"] else ""
        attribution = (f'<p class="muted">Source {label}{published} · First seen {escape(source["first_seen"])}'
                       f' · Last seen {escape(source["last_seen"])}</p>')
        if source["flags"]:
            attribution += '<h3>Posting signals</h3>' + item_list(source["flags"])
    return page(job["title"], f"""
<p class="back"><a href="/?tab=all">All jobs</a></p>
<div class="page-heading">
  <div><h1>{escape(job['title'])}</h1><p class="muted">{escape(job['company'])} ·
     {escape(job['location'] or 'Location unspecified')}</p></div>
  {badge(job['status'], STATUS_LABELS)}
</div>
{feedback(message)}
<div class="detail-grid">
  <div class="stack">
    <section class="panel block fit-panel" aria-labelledby="fit">
      <div class="section-head"><h2 id="fit">Fit assessment</h2><div class="fit fit-large">{fit_cell(job)}</div></div>
      {score_details(job)}
    </section>
    <section class="panel block posting-panel">
      <h2>Posting</h2>
      {attribution}
      {f"<p>{web_link(job['url'], job['url'])}</p>" if job["url"] else ""}
      <div class="posting">{escape(job['description'])}</div>
    </section>
    <section class="panel block history-panel">
      <h2>Status history</h2>
      <ul class="history">{''.join(changes_html)}</ul>
    </section>
  </div>
  <aside class="stack" aria-label="Job actions and contacts">
    <section class="panel block actions-panel">
      <h2>Actions</h2>
      <p class="outreach-link"><a href="/jobs/{job_id}/outreach">Outreach and follow-up</a></p>
      <form action="/jobs/{job_id}/score" method="post">
        {token}<button type="submit" class="button primary">{score_label}</button>
      </form>
      <form action="/jobs/{job_id}/location" method="post" class="form action-form">
        {token}<label>Explicit location<input name="location" value="{escape(job['location'])}"></label>
        <button type="submit" class="button">Save location and re-score</button>
      </form>
      <form action="/jobs/{job_id}/status" method="post" class="form action-form">
        {token}<label>Status<select name="status">{''.join(status_options)}</select></label>
        <label>Reason <span class="muted">(optional)</span><input name="reason"></label>
        {'' if application else '<label>Resume variant <span class="muted">(when marking Applied)</span><input name="variant" maxlength="50" placeholder="e.g. Tailored"></label>'}
        <button type="submit" class="button">Update status</button>
      </form>
    </section>
    <section class="panel block contacts-panel">
      <h2>Contacts</h2><h3>Email</h3>{item_list(contacts['emails'])}
      <h3>LinkedIn profiles</h3>{f'<ul>{profiles}</ul>' if profiles else '<p>—</p>'}
    </section>
    <section class="panel block resume-panel"><h2>Resume</h2>{resume}</section>
    {application_panel(job_id, csrf, application)}
  </aside>
</div>""")


def application_panel(job_id, csrf, application):
    if application is None:
        return ""
    token = csrf_field(csrf)
    received = application["response_at"]
    response = (
        f'<p>Reply recorded {analytics.date(received).astimezone().strftime("%d %b %Y")}.</p>' if received else
        f'<form class="form" action="/jobs/{job_id}/application" method="post">{token}'
        '<input type="hidden" name="action" value="reply">'
        '<button class="button" type="submit">Record application reply</button></form>'
    )
    applied_on = analytics.date(application["applied_at"]).astimezone().strftime("%d %b %Y")
    return f"""<section class="panel block application-panel outreach-actions">
      <h2>Application</h2><p>Sent {applied_on}</p>
      <form class="form" action="/jobs/{job_id}/application" method="post">
        {token}<input type="hidden" name="action" value="variant">
        <label>Resume variant<input name="variant" maxlength="50" required value="{escape(application['variant'])}"></label>
        <button class="button" type="submit">Save resume variant</button>
      </form>{response}</section>"""


def analytics_page(report):
    total, responses = report["applications"], report["responses"]
    if not total:
        return page("Application analytics", """
          <div class="page-heading"><h1>Application analytics</h1></div>
          <section class="panel block"><p>No recorded applications yet. Mark a job as applied to start tracking.</p></section>
        """)
    sections = []
    for key, label in analytics.DIMENSIONS:
        rows = []
        for name, count, replies in report["groups"][key]:
            rows.append(f"""<tr><th scope="row">{escape(name)}</th>
                <td class="number">{count}</td><td class="number">{replies}</td>
                <td class="rate"><meter value="{replies}" max="{count}" aria-hidden="true"></meter>
                <span>{replies / count:.0%}</span></td></tr>""")
        sections.append(f"""<section class="panel stats-panel">
            <h2>{label}</h2>
            <div class="table-scroll"><table aria-label="{label}"><thead><tr><th scope="col">Group</th>
            <th scope="col">Applied</th><th scope="col">Replies</th><th scope="col">Rate</th>
            </tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>""")
    return page("Application analytics", f"""
      <div class="page-heading"><div><h1>Application analytics</h1>
        <p class="muted">Replies include recorded responses, interviews, offers and rejections after applying.</p></div></div>
      <div class="stats-summary">
        <div class="panel block"><span>Applications</span><strong>{total}</strong></div>
        <div class="panel block"><span>Replies</span><strong>{responses}</strong></div>
        <div class="panel block"><span>Response rate</span><strong>{responses / total:.1%}</strong></div>
      </div>
      <div class="stats-grid">{''.join(sections)}</div>
      <p class="muted stats-footnote">Gemini receives aggregate role history after at least {analytics.MIN_SAMPLE}
        applications in that role are {analytics.MATURITY_DAYS} days old.</p>""")


def outreach_form(job_id, csrf, action, label, fields="", primary=False):
    button = "button primary" if primary else "button"
    return f"""<form class="form" action="/jobs/{job_id}/outreach" method="post">
      {csrf_field(csrf)}
      <input type="hidden" name="action" value="{action}">
      {fields}
      <button type="submit" class="{button}">{label}</button>
    </form>"""


def copy_field(label, content, rows=2):
    return f'<label class="copy-field">{label}<textarea rows="{rows}" readonly>{escape(content)}</textarea></label>'


def local_due(sent_at):
    return outreach.due_at(sent_at).astimezone().strftime("%d %b %Y at %H:%M")


def outreach_page(job, row, csrf, message):
    job_id = int(job["id"])
    drafts = json.loads(row["drafts"]) if row else None
    name = escape(drafts["contact"]) if drafts else ""
    people = outreach.contact_names(job["description"])
    contact = f'<p>Named in posting · {escape(", ".join(people))}</p>' if people else ""
    contacts = json.loads(job["contacts"])
    if contacts["emails"]:
        contact += f'<p>Posting emails · {escape(", ".join(contacts["emails"]))}</p>'
    for url in contacts["linkedin_profiles"]:
        contact += f'<p>{web_link(url, url)}</p>'
    contact += '<div class="search-links">' + " ".join(web_link(url, label) for label, url in outreach.search_links(job)) + '</div>'
    sent = row is not None and row["sent_at"] is not None
    draft_form = ""
    if not sent:
        field = f'<label>Contact name <span class="muted">(optional)</span><input name="contact" maxlength="90" value="{name}"></label>'
        draft_form = outreach_form(job_id, csrf, "draft", "Refresh drafts" if drafts else "Draft outreach", field, primary=True)
    drafts_html = ""
    if drafts:
        drafts_html = f"""
        <section class="panel block">
          <h2>Initial drafts</h2>
          {feedback(drafts["warning"])}
          {copy_field("Email subject", drafts["subject"], 1)}
          {copy_field("Email", drafts["email"], 8)}
          {copy_field("LinkedIn connection note", drafts["note"], 3)}
        </section>"""
    tracking = ""
    if drafts and not sent:
        channels = """<label>Channel<select name="channel">
          <option value="email">Email</option><option value="linkedin">LinkedIn</option></select></label>"""
        tracking = outreach_form(job_id, csrf, "sent", "Record as sent", channels)
    elif sent:
        channel = "LinkedIn" if row["channel"] == "linkedin" else "email"
        tracking = f'<p>Initial message recorded via {channel}.</p>'
        if row["replied_at"]:
            tracking += '<p>Reply recorded.</p>'
        elif job["status"] not in outreach.STOP_STATUSES:
            tracking += outreach_form(job_id, csrf, "replied", "Record reply")
        if row["followed_up_at"]:
            tracking += '<p>Follow-up recorded.</p>'
        elif not row["replied_at"] and job["status"] not in outreach.STOP_STATUSES:
            subject, body = outreach.follow_up(job, row)
            due = outreach.clock() >= outreach.due_at(row["sent_at"])
            tracking += f'<h3>Follow-up {"due" if due else "scheduled"} · {local_due(row["sent_at"])}</h3>'
            if row["channel"] == "email":
                tracking += copy_field("Follow-up subject", subject, 1)
            tracking += copy_field("Follow-up draft", body, 6)
            tracking += outreach_form(job_id, csrf, "followed-up", "Record follow-up as sent")
    lifecycle = f'<section class="panel block outreach-actions"><h2>Message tracking</h2>{tracking}</section>' if tracking else ""
    return page("Outreach", f"""
<p class="back"><a href="/jobs/{job_id}">Back to job</a></p>
<div class="page-heading">
  <div><h1>Outreach</h1><p class="muted">{escape(job["title"])} · {escape(job["company"])}</p></div>
</div>
{feedback(message)}
<div class="stack outreach-page">
  <section class="panel block"><h2>Find a contact</h2>{contact}{draft_form}</section>
  {drafts_html}
  {lifecycle}
</div>""")


def follow_ups(jobs):
    rows = "".join(
        f'<li><div><a class="job-title" href="/jobs/{int(job["id"])}/outreach">{escape(job["title"])}</a>'
        f'<span>{escape(local_due(job["sent_at"]))}</span></div><div>{escape(job["company"])}</div></li>'
        for job in jobs
    )
    content = f'<ul class="history">{rows}</ul>' if rows else '<p>No follow-ups due.</p>'
    return page("Follow-ups", f'<div class="page-heading"><h1>Follow-ups</h1></div><section class="panel block">{content}</section>')


def error(message):
    return page("Dashboard error", f'<div class="panel block error-page"><h1>Dashboard error</h1>{feedback(message)}</div>')
