"""Read job alerts from Gmail IMAP without opening any links in the messages."""

import email
import hashlib
import imaplib
import re
import ssl
from email import policy
from email.utils import parseaddr
from urllib.parse import parse_qs

from smartplier.discovery_sources import (
    ALERT_DOMAINS, JobHTML, SourceBatch, SourceError, clean_url, flat, make_posting, subdomain, web_url,
)


MAX_MESSAGE = 2_000_000
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
TEXT_LINK = re.compile(r"https?://[^\s<>]+", re.I)
NO_TITLE = re.compile(r"^(?:view|see|apply|open|learn|read|click)(?:\s|$)|^(?:job|image|here)$", re.I)
ALERT_FIELD = re.compile(r"^(?:title|job title|company|employer|location|job location|work location)\s*:", re.I)
FOOTER = re.compile(r"^(?:unsubscribe|manage (?:your )?(?:email|job|alert)|privacy policy|you received this)\b", re.I)
JOB_PATH = {
    "linkedin": re.compile(r"^/jobs/view/(?:[^/]+-)?(\d+)/?$", re.I),
    "indeed": re.compile(r"^/(?:viewjob|rc/clk|pagead/clk)/?$", re.I),
    "naukri": re.compile(r"^/job-listings-[^/]+-(\d{6,})/?$", re.I),
    "bayt": re.compile(r"^/.+/jobs/[^/]+-(\d+)/?$", re.I),
    "gulftalent": re.compile(r"(?:^|/)jobs/[^/]+-(\d+)/?$", re.I),
    "naukrigulf": re.compile(r"^/[^/]*-jobs-in-[^/]+-(\d{5,})/?$", re.I),
}


def sender_provider(message):
    host = parseaddr(message.get("From", ""))[1].rpartition("@")[2].lower().rstrip(".")
    return next((source for source, domains in ALERT_DOMAINS.items()
                 if any(subdomain(host, domain) for domain in domains)), None)


def alert_link(raw_url, source, unwrap=True):
    url = web_url(raw_url)
    if not url or not any(subdomain(url.hostname, domain) for domain in ALERT_DOMAINS[source]):
        return None
    try:
        query = parse_qs(url.query, max_num_fields=80)
    except ValueError:
        return None
    match = JOB_PATH[source].search(url.path)
    if match:
        if source == "indeed":
            job_id = query.get("jk", [""])[0]
            if re.fullmatch(r"[a-z0-9]{8,32}", job_id, re.I):
                return job_id.lower(), f"https://{url.hostname}/viewjob?jk={job_id.lower()}"
        elif source == "linkedin":
            return match[1], f"https://www.linkedin.com/jobs/view/{match[1]}"
        else:
            return match[1], clean_url(raw_url.split("?", 1)[0])
    elif source == "linkedin" and url.path.rstrip("/") == "/comm/jobs/view":
        job_id = query.get("currentJobId", [""])[0]
        if job_id.isdigit():
            return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"
    elif source == "linkedin":
        direct = re.fullmatch(r"/comm/jobs/view/(?:[^/]+-)?(\d+)/?", url.path)
        if direct:
            return direct[1], f"https://www.linkedin.com/jobs/view/{direct[1]}"
    if unwrap:
        for key in ("url", "u", "redirect", "redirectUrl", "target"):
            if query.get(key):
                target = alert_link(query[key][0], source, unwrap=False)
                if target:
                    return target
    return None


def fragments(part, source):
    body = part.get_content()
    if not isinstance(body, str):
        return []
    if part.get_content_type() == "text/html":
        parser = JobHTML()
        parser.feed(body)
        parser.close()
        text = "".join(parser.parts)
        offsets = [0]
        for fragment in parser.parts:
            offsets.append(offsets[-1] + len(fragment))
        links = [(alert_link(url, source), title, offsets[start], offsets[end])
                 for url, title, start, end in parser.links]
    else:
        text = body
        links = [(alert_link(match[0].rstrip(").,]>"), source), "", match.start(), match.end())
                 for match in TEXT_LINK.finditer(text)]
    links = [link for link in links if link[0]]
    cards = []
    for index, (identity, title, start, end) in enumerate(links):
        following = links[index + 1][2] if index + 1 < len(links) else len(text)
        previous = links[index - 1][3] if index else 0
        before = [flat(line) for line in text[max(previous, start - 250):start].splitlines() if flat(line)]
        after = [flat(line) for line in text[end:min(following, end + 1800)].splitlines() if flat(line)]
        after = after[:next((index for index, line in enumerate(after) if FOOTER.search(line)), len(after))]
        previous_fields = []
        # Plain-text alerts often list the job title immediately above the URL.
        if not title or NO_TITLE.search(title):
            for before_index in range(len(before) - 1, -1, -1):
                line = before[before_index]
                if re.match(r"^(?:title|job title)\s*:", line, re.I):
                    title = line.partition(":")[2].strip()
                    previous_fields = [line for line in before[before_index + 1:] if ALERT_FIELD.search(line)]
                    break
            if not title and before and len(before[-1]) <= 140 and not re.search(r":|https?://", before[-1], re.I):
                title = before[-1]
        cards.append((identity, title, previous_fields + after))
    return cards


def alert_postings(data):
    message = email.message_from_bytes(data, policy=policy.default)
    source = sender_provider(message)
    if source is None:
        return []
    # Prefer labelled HTML anchors, then use the alternative plain-text body.
    identities = {}
    for kind in ("html", "plain"):
        part = message.get_body(preferencelist=(kind,))
        if part is None:
            continue
        for (job_id, url), title, lines in fragments(part, source):
            if not title or NO_TITLE.search(title) or len(title) > 180:
                continue
            company = location = ""
            for line in lines[:5]:
                field, _, value = line.partition(":")
                if field.casefold() in {"company", "employer"}:
                    company = value.strip()
                if field.casefold() in {"location", "job location", "work location"}:
                    location = value.strip()
            if not company and lines and " · " in lines[0]:
                employer, place, *_ = lines[0].split(" · ")
                if len(employer) <= 100 and len(place) <= 100 and not place.lower().endswith(" ago"):
                    company, location = employer, location or place
            if not company and " at " in title:
                title, company = title.rsplit(" at ", 1)
            candidate = make_posting(title, company, location, "\n".join(lines[:6]), url, f"{source}:{job_id}")
            previous = identities.get(job_id)
            quality = lambda item: (bool(item.location), item.company != "Unknown company", bool(item.description))
            if previous is None or quality(candidate) > quality(previous):
                identities[job_id] = candidate
    return list(identities.values())


def gmail_batch(options, user, password, seen=None):
    password = "".join(password.split())
    if not re.fullmatch(r"[a-zA-Z0-9]{16}", password):
        raise SourceError("GMAIL_APP_PASSWORD must be the 16-character app password from your Google Account.")
    batch = SourceBatch("gmail", "Gmail job alerts")
    domains = " ".join(f"from:{domain}" for group in ALERT_DOMAINS.values() for domain in group)
    query = f"newer_than:{options['days']}d {{{domains}}}"
    step = "connect"
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=20) as inbox:
            step = "sign in"
            inbox.login(user, password)
            step = "open the mailbox"
            state, _ = inbox.select(f'"{options["mailbox"]}"', readonly=True)
            if state != "OK":
                raise SourceError("Gmail could not open the configured mailbox.")
            step = "search for job alerts"
            state, data = inbox.uid("search", None, "X-GM-RAW", f'"{query}"')
            if state != "OK":
                raise SourceError("Gmail could not search for alerts.")
            step = "read a job alert"
            uids = (next(iter(data or ()), None) or b"").split()
            batch.checked = len(uids)
            if seen is not None:
                validity = (inbox.response("UIDVALIDITY")[1] or [None])[0]
                if not validity:
                    raise SourceError("Gmail did not provide a mailbox identity for incremental sync.")
                scope = hashlib.sha256(repr((user.casefold(), options["mailbox"], validity)).encode()).hexdigest()
                selected = [(uid, scope + ":" + uid.decode("ascii")) for uid in uids if uid.isdigit()]
                selected = [(uid, key) for uid, key in selected if key not in seen][:options["limit"]]
            else:
                selected = [(uid, None) for uid in uids[-options["limit"]:]]
            for uid, key in selected:
                state, body = inbox.uid("fetch", uid, f"(BODY.PEEK[]<0.{MAX_MESSAGE}>)")
                data = next((item[1] for item in body or [] if isinstance(item, tuple)), b"")
                if state != "OK" or not data or len(data) >= MAX_MESSAGE:
                    batch.skipped += 1
                    if key and state == "OK" and len(data) >= MAX_MESSAGE:
                        batch.message_ids.append(key)
                    continue
                try:
                    postings = alert_postings(data)
                except (LookupError, ValueError, AttributeError, TypeError):
                    postings = []
                batch.postings.extend(postings)
                batch.skipped += not postings
                if key:
                    batch.message_ids.append(key)
            step = "close the IMAP session"
    except ssl.SSLCertVerificationError:
        raise SourceError(f"TLS certificate verification failed for {IMAP_HOST}:{IMAP_PORT}.") from None
    except (imaplib.IMAP4.abort, OSError):
        raise SourceError(f"IMAP connection to {IMAP_HOST}:{IMAP_PORT} failed while trying to {step}.") from None
    except imaplib.IMAP4.error:
        if step == "sign in":
            raise SourceError("Gmail rejected the sign-in. Use an app password generated for the same account as GMAIL_USER.") from None
        raise SourceError(f"Gmail IMAP could not {step}.") from None
    return batch
