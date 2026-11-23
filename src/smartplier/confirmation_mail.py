"""Read-only matching of application receipts in Gmail."""
import hashlib
import re
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlsplit

from smartplier.analytics import date
from smartplier.ats_detection import detect
from smartplier.discovery_mail import IMAP_HOST, IMAP_PORT, imaplib, ssl
from smartplier.generic_application import CONFIRMATION

RECEIPT = re.compile(r"application.{0,35}(?:received|submitted|confirmation)|thank(?:s| you).{0,18}applying", re.I)
TRUSTED_SENDERS = {
    "Lever": ("lever.co",), "Workable": ("workable.com", "workablemail.com"),
    "Greenhouse": ("greenhouse.io", "greenhouse-mail.io"), "Ashby": ("ashbyhq.com",),
}


def words(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def matches_receipt(message, application, recipient):
    sender, email = parseaddr(str(message.get("From", "")))
    host = email.rpartition("@")[2].lower()
    received = str(message.get("Delivered-To", "") or message.get("To", ""))
    if parseaddr(received)[1].casefold() != recipient.casefold():
        return False
    subject = str(message.get("Subject", ""))
    if not RECEIPT.search(subject):
        return False
    try:
        when = message["Date"].datetime
        start = date(application["reserved_at"])
        if not start - timedelta(minutes=2) <= when <= start + timedelta(days=3):
            return False
    except (TypeError, ValueError, AttributeError):
        return False
    body = message.get_body(preferencelist=("plain", "html"))
    try:
        content = body.get_content()[:80_000] if body else ""
    except (LookupError, ValueError, TypeError):
        return False
    employer, role = words(application["company"]), words(application["title"])
    text = " " + words(subject + " " + sender + " " + content) + " "
    if not employer or not role or " " + role + " " not in text or " " + employer + " " not in text:
        return False
    posting = application["url"]
    ats = detect(posting).platform
    source_host = urlsplit(posting).hostname
    approved = TRUSTED_SENDERS.get(ats, ()) or ((source_host,) if source_host else ())
    if not host or not any(host == domain or host.endswith("." + domain) for domain in approved):
        return False
    authentication = " ".join(str(x) for x in message.get_all("Authentication-Results", [])[:1]).casefold()
    # Gmail-added header must authenticate the same From domain, not merely any domain.
    if (not authentication.startswith("mx.google.com;")
            or not re.search(r"\bdmarc=pass[^;]*\bheader\.from=" + re.escape(host) + r"(?:[\s;]|$)", authentication)):
        return False
    if ats in TRUSTED_SENDERS:
        # Roles can be reposted with the same name. On a shared board require the exact job URL/path.
        parsed = urlsplit(posting)
        path = parsed.path.rstrip("/").removesuffix("/apply").removesuffix("/application")
        if not path or not re.search(re.escape(parsed.hostname + path) + r"(?:[/?\s#\"'<]|$)", content, re.I):
            return False
    return bool(CONFIRMATION.search(content) or RECEIPT.search(subject))


def find_receipts(user, password, pending):
    if not pending or not user or not re.fullmatch(r"[A-Za-z0-9]{16}", "".join(password.split())):
        return {}
    found = {}
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=15) as inbox:
        inbox.login(user, "".join(password.split()))
        state, _ = inbox.select("INBOX", readonly=True)
        if state != "OK":
            raise ValueError("Gmail could not open the confirmation inbox.")
        state, data = inbox.uid("search", None, "X-GM-RAW", '"newer_than:4d {subject:application subject:applying}"')
        if state != "OK":
            raise ValueError("Gmail could not search for application confirmations.")
        for uid in (data[0] or b"").split()[-100:]:
            if not uid.isdigit():
                continue
            state, results = inbox.uid("fetch", uid, "(BODY.PEEK[]<0.150000>)")
            raw = next((item[1] for item in results or [] if isinstance(item, tuple)), b"")
            if state != "OK" or not raw or len(raw) >= 150_000:
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw)
            matching = [row["job_id"] for row in pending if matches_receipt(message, row, user)]
            if len(matching) == 1:
                found[matching[0]] = hashlib.sha256(str(message.get("Message-ID", uid)).encode()).hexdigest()
    return found
