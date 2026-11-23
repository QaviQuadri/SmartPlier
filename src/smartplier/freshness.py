"""Read-only checks against official ATS board and application endpoints."""
import re
from dataclasses import dataclass
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from smartplier.ats_detection import detect
from smartplier.form_engine import NeedsReview, PolicyBlocked
from smartplier.prefill import lever_apply_url
from smartplier.discovery_sources import BOARDS, FetchRedirect, SourceError, fetch

CLOSED = re.compile(r"\b(?:this (?:job|position) (?:is )?(?:no longer available|closed)|"
                    r"no longer accepting applications|applications (?:for this (?:role|job) )?(?:are |have )?closed|"
                    r"job (?:not found|has expired))\b", re.I)


def application_access(url, host):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise NeedsReview("The official board did not return a recognized application address.")
    try:
        with build_opener(FetchRedirect()).open(Request(url), timeout=15) as response:
            final = urlsplit(response.geturl())
            if final.hostname != host or final.scheme != "https":
                raise NeedsReview("The application redirected outside its approved ATS host.")
            # An HTTP 200 error, CAPTCHA, or generic careers page is not an active application.
            if not re.search(r"/apply/?$", final.path):
                raise NeedsReview("The ATS did not open the application page.")
            expected = parsed.path.rstrip("/").split("/")[-2].casefold()
            if expected not in {p.casefold() for p in final.path.split("/")}:
                raise NeedsReview("The ATS did not open the expected listing.")
            final_url = final.geturl()
            body = response.read(1_500_000).decode("utf-8", errors="replace")
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (404, 410):
            raise PolicyBlocked(f"The official application page is unavailable (HTTP {code}).") from None
        raise NeedsReview(f"The application page could not be verified (HTTP {code}).") from None
    except (URLError, OSError):
        raise NeedsReview("The application page could not be reached.") from None
    text = re.sub(r"<(script|style)[\s\S]*?</\1\s*>", " ", body, flags=re.I)
    text = " ".join(unescape(re.sub(r"<[^>]*>", " ", text)).split())
    if CLOSED.search(text):
        raise PolicyBlocked("The employer application page explicitly says the posting is closed.")
    if re.search(r"verify (?:that )?you are human|complete the captcha|access denied", text, re.I):
        raise NeedsReview("An ATS access check prevented verification.")
    return final_url


@dataclass(frozen=True)
class Verification:
    message: str
    application_url: str


def workable_listing(job, aliases):
    path = urlsplit(job["url"]).path
    listing = re.fullmatch(r"/(?:([a-zA-Z0-9_-]+)/)?j/([a-zA-Z0-9]+)(?:/apply)?/?", path)
    if not listing:
        raise NeedsReview("The Workable posting ID could not be identified.")
    code = listing[2].upper()
    boards = {parts[2] for alias in aliases if re.fullmatch(r"id:workable:[a-zA-Z0-9_-]+:[a-zA-Z0-9]+", alias)
              and (parts := alias.split(":"))[3].upper() == code}
    if listing[1]:
        boards.add(listing[1])
    if len(boards) != 1:
        raise NeedsReview("The employer's Workable board is not uniquely identified.")
    board = boards.pop()
    payload = fetch(BOARDS["workable"].format(board=board), "json")
    listings = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(listings, list):
        raise NeedsReview("The official Workable board did not return a posting list.")
    row = next((item for item in listings if isinstance(item, dict) and str(item.get("shortcode")).upper() == code), None)
    ended = {"closed", "expired", "archived", "unpublished", "draft", "inactive", "removed"}
    if not row or any(str(row.get(field, "")).strip().lower() in ended for field in ("status", "state")):
        raise PolicyBlocked("The listing is removed or closed on the employer's official Workable board.")
    target = row.get("application_url") or f"https://apply.workable.com/{board}/j/{code}/apply/"
    if code not in {part.upper() for part in urlsplit(target).path.split("/")}:
        raise NeedsReview("The official Workable application link does not match the posting ID.")
    return target


def lever_listing(job, _aliases):
    target = lever_apply_url(job["url"])
    url = urlsplit(target)
    board, ident = url.path.strip("/").split("/")[:2]
    provider = "lever-eu" if url.hostname == "jobs.eu.lever.co" else "lever"
    listings = fetch(BOARDS[provider].format(board=board), "json")
    if not isinstance(listings, list):
        raise NeedsReview("The official Lever board did not return a posting list.")
    if not any(isinstance(row, dict) and str(row.get("id")).casefold() == ident.casefold() for row in listings):
        raise PolicyBlocked("The listing is no longer on the employer's official Lever board.")
    return target


BOARD_CHECKS = {"Lever": lever_listing, "Workable": workable_listing}


def verify(job, aliases):
    detected = detect(job["url"])
    check = BOARD_CHECKS.get(detected.platform)
    if not check or detected.ambiguity:
        raise NeedsReview(f"Official listing verification for {detected.platform} is unavailable for this URL.")
    try:
        target = check(job, aliases)
        host = urlsplit(job["url"]).hostname
        final = application_access(target, host)
    except SourceError:
        raise NeedsReview("The official ATS board could not be verified.") from None
    return Verification(f"Verified on the employer's official {detected.platform} board and application page.", final)
