"""Read job data from public JSON boards, Adzuna and remote RSS feeds."""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "lever-eu": "https://api.eu.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
    "workable": "https://www.workable.com/api/accounts/{board}?details=true",
}
ALERT_DOMAINS = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com", "indeed.co.in", "indeedemail.com"),
    "naukri": ("naukri.com",),
    "bayt": ("bayt.com",),
    "gulftalent": ("gulftalent.com",),
    "naukrigulf": ("naukrigulf.com",),
}
MAX_RESPONSE = 16_000_000
TRACKING = {"trk", "trackingid", "ref", "refid", "source", "src", "gh_src", "fbclid", "gclid"}
SECRET_PARTS = {"app_id", "app_key", "api_key", "apikey", "key", "password", "token", "access_token"}
BLOCK = {"br", "p", "div", "li", "section", "article", "tr", "td", "h1", "h2", "h3", "h4", "ul", "ol"}
MARKUP = re.compile(r"</?[a-zA-Z][^>]*>")


class SourceError(ValueError):
    """A discovery error containing no credentials or fetch URLs."""


@dataclass(frozen=True)
class Posting:
    title: str
    company: str
    location: str
    description: str
    url: str
    external_id: str = ""
    published_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class SourceBatch:
    key: str
    label: str
    postings: list = field(default_factory=list)
    skipped: int = 0
    checked: int = 0
    message_ids: list[str] = field(default_factory=list)


def subdomain(host, domain):
    host = host.rstrip(".")
    return host == domain or host.endswith("." + domain)


def web_url(value, *, https_only=False):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    schemes = ("https",) if https_only else ("https", "http")
    if (parsed.scheme not in schemes or not host or not re.fullmatch(r"[a-z0-9.-]+", host)
            or parsed.username is not None or parsed.password is not None
            or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value.strip())
            or port not in (None, 443 if parsed.scheme == "https" else 80)):
        return None
    return parsed


def clean_url(value):
    url = web_url(value)
    if url is None:
        return ""
    try:
        query = parse_qsl(url.query, keep_blank_values=True, max_num_fields=120)
    except ValueError:
        return ""
    params = sorted((name, val) for name, val in query
                    if name.casefold() not in TRACKING | SECRET_PARTS and not name.casefold().startswith("utm_"))
    return urlunsplit((url.scheme, url.hostname.rstrip("."), url.path.rstrip("/") or "/", urlencode(params), ""))


class FetchRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = web_url(req.full_url, https_only=True)
        new = web_url(newurl, https_only=True)
        if old and new and (
            old.hostname == new.hostname
            or subdomain(old.hostname, "workable.com") and subdomain(new.hostname, "workable.com")
        ) and old.hostname != "api.adzuna.com":
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        return None


def fetch(url, media):
    parsed = web_url(url, https_only=True)
    blocked = {domain for domains in ALERT_DOMAINS.values() for domain in domains}
    if not parsed or any(subdomain(parsed.hostname, domain) for domain in blocked):
        raise SourceError("Only HTTPS public boards and feeds from permitted sites can be fetched.")
    accept = "application/json" if media == "json" else "application/rss+xml, application/atom+xml, application/xml, text/xml"
    request = Request(parsed.geturl(), headers={"Accept": accept, "User-Agent": "Smartplier/1.0"})
    try:
        with build_opener(FetchRedirect()).open(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            ok = (content_type == "application/json" or content_type.endswith("+json")) if media == "json" else (
                content_type in {"application/xml", "text/xml"} or content_type.endswith("+xml"))
            if not ok:
                raise SourceError("Source returned an unexpected content type.")
            data = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        code = error.code
        error.close()
        raise SourceError(f"Source returned HTTP {code}.") from None
    except (URLError, OSError):
        raise SourceError("Source could not be reached.") from None
    if len(data) > MAX_RESPONSE:
        raise SourceError("Source response exceeded the size limit.")
    if media == "json":
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise SourceError("Source returned invalid JSON.") from None
    # Reject DTDs and entity declarations. Decoding first also handles UTF-16 XML.
    try:
        xml = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
            raise SourceError("RSS feed attempted to declare an XML document type or entity.")
        return ET.fromstring(xml)
    except (ET.ParseError, UnicodeError):
        raise SourceError("Source returned invalid RSS or Atom XML.") from None


def flat(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


class JobHTML(HTMLParser):
    def __init__(self, keep_links=False):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.links = []
        self.anchor = None
        self.keep_links = keep_links

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "a":
            self.anchor = (dict(attrs).get("href") or "", len(self.parts))

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "a" and self.anchor:
            url, start = self.anchor
            label = flat("".join(self.parts[start:]))
            self.links.append((url, label, start, len(self.parts)))
            if self.keep_links:
                parsed = web_url(url)
                if url.startswith("mailto:"):
                    self.parts.append(" " + url[7:].split("?")[0] + " ")
                elif parsed and subdomain(parsed.hostname, "linkedin.com") and parsed.path.startswith("/in/"):
                    self.parts.append(" " + clean_url(url) + " ")
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    if not isinstance(value, str):
        return ""
    if not MARKUP.search(value) and "&lt;" in value:
        value = unescape(value)
    if MARKUP.search(value):
        parser = JobHTML(keep_links=True)
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    else:
        value = unescape(value)
    return "\n".join(line for raw in value.splitlines() if (line := flat(raw)))


def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            date = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc)
        elif isinstance(value, str) and value:
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                date = parsedate_to_datetime(value)
        else:
            return None
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def location_field(value):
    if isinstance(value, list):
        return " / ".join(dict.fromkeys(filter(None, map(location_field, value))))
    if isinstance(value, dict):
        area = value.get("area")
        area = area if isinstance(area, list) else []
        return (flat(value.get("location_str")) or flat(value.get("display_name") if not area else None)
                or ", ".join(filter(None, (flat(value.get(key)) for key in ("city", "region", "country"))))
                or ", ".join(flat(part) for part in reversed(area) if flat(part)))
    return flat(value)


def remote_location(location, is_remote):
    if not is_remote:
        return location
    if not location:
        # A workplace type alone cannot establish where the employer hires.
        return "Remote (eligibility unspecified)"
    return " / ".join(part if re.search(r"\bremote\b", part, re.I) else f"Remote - {part}"
                      for part in location.split(" / "))


def make_posting(title, company, location, description, url, ident="", date=None, expiry=None):
    return Posting(flat(unescape(title))[:300] if isinstance(title, str) else "",
                   flat(unescape(company))[:300] if isinstance(company, str) and company.strip() else "Unknown company",
                   location_field(location)[:500], plain(description)[:40_000], clean_url(url),
                   str(ident or "")[:500], timestamp(date), timestamp(expiry))


def rows(payload, key):
    data = payload if key is None else payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise SourceError("Source JSON did not contain a valid job list.")
    return data


def ats_posting(provider, company, job):
    if provider == "greenhouse":
        return make_posting(job.get("title"), company, location_field(job.get("location", {}).get("name"))
                            if isinstance(job.get("location"), dict) else job.get("location"),
                            job.get("content"), job.get("absolute_url"), job.get("id"), job.get("first_published"))
    if provider in {"lever", "lever-eu"}:
        loc = job.get("categories") or {}
        location = location_field(loc.get("allLocations") or loc.get("location"))
        description = "\n".join(str(job.get(key) or "") for key in ("descriptionPlain", "additionalPlain"))
        if not description.strip():
            description = job.get("description")
        lists = "\n".join(plain(item.get("text")) + "\n" + plain(item.get("content"))
                          for item in job.get("lists") or [] if isinstance(item, dict))
        return make_posting(job.get("text"), company,
                            remote_location(location, job.get("workplaceType") == "remote"),
                            f"{description or ''}\n{lists}", job.get("hostedUrl"), job.get("id"), job.get("createdAt"))
    if provider == "ashby":
        if job.get("isListed") is False:
            return None
        location = location_field([job.get("location"), *(item.get("location") for item in
                                   job.get("secondaryLocations") or [] if isinstance(item, dict))])
        remote = job.get("isRemote") is True or str(job.get("workplaceType")).lower() == "remote"
        return make_posting(job.get("title"), company, remote_location(location, remote),
                            job.get("descriptionPlain") or job.get("descriptionHtml"), job.get("jobUrl"),
                            job.get("id"), job.get("publishedAt"))
    published_places = job.get("locations")
    if isinstance(published_places, list) and published_places:
        location = location_field([place for place in published_places
                                   if not (isinstance(place, dict) and place.get("hidden"))])
    else:
        location = location_field(job.get("location") or job)
    remote = job.get("remote") is True or job.get("telecommuting") is True
    return make_posting(job.get("title"), company, remote_location(location, remote),
                        job.get("description") or job.get("full_description"),
                        job.get("shortlink") or job.get("url") or job.get("application_url"),
                        job.get("shortcode") or job.get("id"),
                        job.get("published_on") or job.get("published_at") or job.get("created_at"))


def ats_batch(entry):
    provider, board, company = entry["provider"], entry["board"], entry["company"]
    jobs = rows(fetch(BOARDS[provider].format(board=board), "json"),
                None if provider in {"lever", "lever-eu"} else "jobs")
    batch = SourceBatch(f"{provider}:{board}", f"{company} ({provider})")
    for job in jobs:
        try:
            post = ats_posting(provider, company, job)
        except (AttributeError, TypeError, ValueError):
            batch.skipped += 1
            continue
        if post is None:
            continue
        if post.title and (post.external_id or post.url):
            batch.postings.append(post)
        else:
            batch.skipped += 1
    return batch


def adzuna_batch(options, app_id, app_key):
    batch = SourceBatch("adzuna:in", "Adzuna India")
    for query in options["queries"]:
        for page in range(1, options["pages"] + 1):
            params = urlencode({"app_id": app_id, "app_key": app_key, "what": query,
                                "results_per_page": options["results_per_page"]})
            jobs = rows(fetch(f"https://api.adzuna.com/v1/api/jobs/in/search/{page}?{params}", "json"), "results")
            for job in jobs:
                company = job.get("company")
                post = make_posting(job.get("title"), company.get("display_name") if isinstance(company, dict) else "",
                                    job.get("location"), job.get("description"), job.get("redirect_url"),
                                    job.get("id"), job.get("created"))
                if post.title and (post.external_id or post.url):
                    batch.postings.append(post)
                else:
                    batch.skipped += 1
            if len(jobs) < options["results_per_page"]:
                break
    return batch


def rss_batch(entry):
    url = entry["url"]
    feed = fetch(url, "xml")
    tag = lambda node: node.tag.rsplit("}", 1)[-1]
    if tag(feed) not in {"rss", "feed"}:
        raise SourceError("Expected an RSS or Atom feed.")
    himalayas = web_url(url).hostname == "himalayas.app"
    batch = SourceBatch(f"rss:{url}", entry["name"])
    for node in feed.iter():
        if tag(node) not in {"item", "entry"}:
            continue
        fields = {}
        for child in node:
            name = tag(child)
            value = child.text or ""
            if name == "link":
                if child.get("rel", "alternate") != "alternate":
                    continue
                value = child.get("href", value)
            fields.setdefault(name, []).append(value)

        def first(*names):
            return next((fields[name][0] for name in names if fields.get(name) and fields[name][0].strip()), "")

        restrictions = list(filter(None, map(flat, fields.get("locationRestriction", []))))
        location = " / ".join(restrictions) or first("region", "location", "country")
        if himalayas and not location:
            location = "Remote (timezone restrictions)" if fields.get("timezoneRestriction") else "Remote worldwide"
        else:
            location = remote_location(location, bool(restrictions))
        post = make_posting(first("title"), first("companyName", "company", "creator"), location,
                            first("encoded", "content", "description", "summary"), first("link"),
                            first("guid", "id"), first("pubDate", "published"), first("expiryDate"))
        if post.title and post.url:
            batch.postings.append(post)
        else:
            batch.skipped += 1
    return batch
