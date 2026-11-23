"""Read-only ATS identification from application URLs and observed browser evidence."""

import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit

from smartplier.prefill import HOSTS, POSTING


@dataclass(frozen=True)
class Platform:
    name: str
    hosts: tuple
    pattern: str


REGISTRY = {
    "lever": Platform("Lever", HOSTS, POSTING.pattern),
    "greenhouse": Platform("Greenhouse", ("boards.greenhouse.io", "job-boards.greenhouse.io",
                           "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"), r"/[^/]+/jobs/\d+/?"),
    "workable": Platform("Workable", ("apply.workable.com",), r"/(?:[a-zA-Z0-9_-]+/)?j/[a-zA-Z0-9]+(?:/apply)?/?"),
    "ashby": Platform("Ashby", ("jobs.ashbyhq.com",), r"/[^/]+/[a-fA-F0-9-]{36}(?:/application)?/?"),
    "smartrecruiters": Platform("SmartRecruiters", ("jobs.smartrecruiters.com",), r"/[^/]+/[^/]+/?"),
    "breezy": Platform("Breezy", (), r"/p/[^/]+(?:/apply)?/?"),
    "workday": Platform("Workday", (), r"/.+/(?:job|apply)/.+"),
}


@dataclass(frozen=True)
class Detection:
    platform: str
    handler: str | None
    confidence: str
    evidence: tuple
    final_url: str | None
    verification: str
    ambiguity: str | None

    def as_dict(self):
        return asdict(self)


def secure_url(value):
    try:
        if not isinstance(value, str) or "\\" in value or any(c.isspace() or not c.isprintable() for c in value):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None):
            return parsed
    except ValueError:
        pass
    return None


def platform_key(parsed):
    for key, platform in REGISTRY.items():
        if parsed.hostname in platform.hosts:
            return key
    if parsed.hostname.endswith(".myworkdayjobs.com"):
        return "workday"
    if parsed.hostname.endswith(".breezy.hr"):
        return "breezy"
    return None


def posting_path(key, parsed):
    if key == "greenhouse" and parsed.path == "/embed/job_app":
        try:
            return any(value.isdigit() for value in parse_qs(parsed.query, max_num_fields=20).get("token", []))
        except ValueError:
            return False
    return re.fullmatch(REGISTRY[key].pattern, parsed.path) is not None


def detect(original, *, observed_url=None, official_listing=False, form_inspected=False):
    original_url = secure_url(original)
    candidate = secure_url(observed_url if observed_url is not None else original)
    if not candidate:
        return Detection("Unknown", None, "none", (), None, "unverified", "No safe HTTPS application URL.")
    key = platform_key(candidate)
    evidence = []
    start = platform_key(original_url) if original_url else None
    if original_url and observed_url is not None and original_url.geturl() != candidate.geturl():
        evidence.append(f"Observed navigation from {original_url.hostname} to {candidate.hostname}.")
    if official_listing:
        evidence.append("The official board returned the exact posting.")
    if form_inspected:
        evidence.append("Application form semantics were inspected.")
    if not key:
        return Detection("Unknown", "generic-inspector" if form_inspected else None,
                         "low", tuple(evidence), candidate.geturl(), "form inspected" if form_inspected else "unverified",
                         "A page structure alone does not establish the employer or a submission handler.")
    info = REGISTRY[key]
    evidence.append(f"Recognized {info.name} application host.")
    matched = posting_path(key, candidate)
    if matched:
        evidence.append("URL matches the platform posting pattern.")
    ambiguous = start and start != key
    return Detection(info.name, None if ambiguous else "generic-inspector",
                     "high" if matched and not ambiguous else "medium", tuple(evidence), candidate.geturl(),
                     "official listing and form inspected" if official_listing and form_inspected else
                     "official listing and page verified" if official_listing and observed_url else
                     "official listing verified" if official_listing else "form inspected" if form_inspected else "unverified",
                     "The URL changed to another ATS. Verify the destination." if ambiguous else
                     None if matched else "Recognized host, unfamiliar posting URL.")
