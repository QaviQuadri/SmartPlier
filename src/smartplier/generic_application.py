"""Generic application-form discovery and browser guards."""
import http.client
import hashlib
import ipaddress
import re
import socket
import ssl
from email import policy
from email.parser import BytesParser
from urllib.parse import urljoin, urlsplit

from smartplier.ats_detection import secure_url
from smartplier.form_engine import FORM_SNAPSHOT, NeedsReview, PolicyBlocked
from smartplier.freshness import CLOSED

JOB_BOARD_LABELS = {"linkedin", "indeed", "indeedemail", "naukri", "naukrigulf", "bayt", "gulftalent"}
ACCESS_CHECK = re.compile(r"verify (?:that )?you are human|complete the captcha|access denied|"
                          r"unusual traffic|enter (?:your )?(?:verification|security) code", re.I)
CONFIRMATION = re.compile(r"application (?:has been |was )?(?:successfully )?submitted|"
                          r"thank you for (?:applying|your application)|thanks for applying|"
                          r"we(?: have|'ve) received your application", re.I)
LEGAL_TEXT = re.compile(r"by (?:clicking|submitting)|attest|i certify|i agree|i acknowledge|i consent|"
                        r"declare that|terms (?:of use|and conditions)", re.I)
PRIVACY = re.compile(
    r"(?:i )?(?:(?:have )?read(?: and (?:accept|understand))?|accept|agree to|acknowledge|understand) "
    r"(?:(?:the|your|our) )?(?:(?:candidate|applicant|recruitment) )?privacy (?:policy|notice|statement)"
    r"|(?:i )?(?:consent to|agree to) (?:the )?processing of my personal data "
    r"(?:for this application|for recruitment purposes)", re.I)
NEXT = re.compile(r"^(?:next|continue)(?:\s+(?:step|to application))?$", re.I)
APPLY = re.compile(r"^(?:apply|apply now|apply for (?:this|the) (?:job|role|position)|start application)$", re.I)
SUBMIT = "button[type=submit], input[type=submit], button:not([type])"
BODY_FIELDS = """(f, submit) => [...new FormData(f, f.querySelector(submit)).entries()].map(([k,v]) => [k,
 typeof v === 'string' ? v : (v.name ? {file:v.name,size:v.size} : {file:''}),
 [...f.elements].some(e => e.name === k && e.type === 'hidden')])"""
SECRET = re.compile(r"password|csrf|token|secret|session|authenticity", re.I)
POSTING_FIELD = re.compile(r"job|posting|position|requisition|vacancy|employer|company|target", re.I)


class Assisted(NeedsReview):
    pass


def origin(url):
    parsed = secure_url(url)
    if parsed and "." in parsed.hostname and not (set(parsed.hostname.split(".")) & JOB_BOARD_LABELS):
        return parsed.hostname
    return None


def public_address(host):
    if host is None or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise PolicyBlocked("The application URL has no permitted public HTTPS host.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PolicyBlocked("Use a public employer hostname instead of an IP address.")
    try:
        addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        raise Assisted("The employer hostname could not be resolved.") from None
    if not addresses or not all(ip.is_global and not ip.is_multicast for ip in addresses):
        raise PolicyBlocked("The employer hostname resolves to a restricted network.")
    return min(addresses, key=lambda address: (address.version, int(address)))


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        tcp = socket.create_connection((str(self.address), 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(tcp, server_hostname=self.host)
        except BaseException:
            tcp.close()
            raise


class Gate:
    """Native Chromium requests use the pinned public address, including redirects."""
    def __init__(self, target, *, fixture=False):
        self.host = origin(target) if not fixture else urlsplit(target).netloc
        self.address = public_address(self.host) if not fixture else None
        ip = self.address
        self.args = ([f"--host-resolver-rules=MAP {self.host} {ip if ip.version == 4 else '[' + str(ip) + ']'}, MAP * ~NOTFOUND",
                      "--no-proxy-server", "--disable-quic"] if ip else [])
        self.fixture = fixture
        self.endpoint = None
        self.sent = False
        self.status = None
        self.blocked_post = False
        self.before_post = None
        self.review_error = None
        self.redirects = {}

    def allows(self, url):
        return origin(url) == self.host and self.host is not None if not self.fixture else (
            urlsplit(url).scheme == "http" and urlsplit(url).netloc == self.host
        )

    def route(self, route):
        req = route.request
        is_post = req.method == "POST"
        chain = req
        while chain.redirected_from:
            chain = chain.redirected_from
        permitted_chain = self.allows(chain.url)
        if is_post:
            valid = (not self.sent and self.endpoint == req.url and req.is_navigation_request()
                     and self.allows(req.url) and permitted_chain and not req.redirected_from)
            if not valid:
                self.blocked_post = True
                route.abort()
                return
            if self.before_post:
                try:
                    self.before_post(req)
                except (ValueError, TypeError):
                    self.review_error = "The manual form could not be reserved safely. Check its eligibility and payload."
                    route.abort()
                    return
            self.sent = True
            self.forward(route, req)
            return
        elif req.method not in {"GET", "HEAD"} or not self.allows(req.url) or not permitted_chain:
            route.abort()
            return
        self.forward(route, req)

    def forward(self, route, req):
        target = urlsplit(req.url)
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        client = (http.client.HTTPConnection(target.hostname, target.port, timeout=15) if self.fixture
                  else PinnedHTTPS(self.host, self.address))
        try:
            headers = {key: value for key, value in req.all_headers().items()
                       if key not in {"host", "connection", "content-length", "transfer-encoding", "accept-encoding"}}
            method = req.method
            client.request(method, path, body=req.post_data_buffer if method == "POST" else None, headers=headers)
            with client.getresponse() as response:
                if method == "POST":
                    self.status = response.status
                location = response.getheader("location")
                if response.status in {301, 302, 303, 307, 308}:
                    destination = urljoin(req.url, location or "")
                    if not location or not self.allows(destination) or (
                        method == "POST" and response.status not in {302, 303}
                    ):
                        if method == "POST":
                            self.blocked_post = True
                            route.abort()
                        else:
                            route.fulfill(status=403, content_type="text/plain", body="Unapproved employer redirect")
                        return
                    if req.is_navigation_request():
                        self.redirects[req.url] = destination
                    headers = {"content-type": "text/plain; charset=utf-8"}
                    if session_cookie := response.getheader("Set-Cookie"):
                        headers["set-cookie"] = session_cookie
                    # The browser never follows location headers itself. A guarded GET opens the
                    # approved destination explicitly, so redirect chains cannot resend a POST.
                    route.fulfill(status=200, headers=headers, body="Employer redirect")
                    return
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    route.abort()
                    return
                safe_headers = {key: value for key, value in response.getheaders()
                                if key.casefold() not in {"connection", "transfer-encoding", "content-length"}}
                route.fulfill(status=response.status, headers=safe_headers, body=body)
        except (OSError, http.client.HTTPException):
            route.abort()
        finally:
            client.close()

    def install(self, context):
        context.route("**/*", self.route)


def check_access(page, gate, response=None):
    if response is not None and response.status in {401, 403, 429}:
        raise Assisted(f"The employer returned an access check (HTTP {response.status}).")
    if response is not None and response.status in {404, 410}:
        raise PolicyBlocked("The employer application page is closed or removed.")
    if response is not None and not response.ok:
        raise Assisted("The application page could not be opened.")
    if not gate.allows(page.url):
        raise Assisted("The application navigated outside the approved employer origin.")
    challenge = '[data-sitekey], [name=g-recaptcha-response], [name=h-captcha-response], input[autocomplete=one-time-code], input[type=password]'
    for frame in page.frames:
        if frame != page.main_frame and not gate.allows(frame.url):
            raise Assisted("A cross-origin embedded form requires assisted review.")
        if frame.locator(challenge).count():
            raise Assisted("A CAPTCHA, sign-in, or verification code requires assisted review.")
    body = page.locator("body").inner_text(timeout=5000)
    if CLOSED.search(body):
        raise PolicyBlocked("The employer says this posting is closed.")
    if ACCESS_CHECK.search(body):
        raise Assisted("An employer access check requires assisted review.")


def open_page(page, gate, url):
    for _ in range(4):
        if not gate.allows(url):
            raise Assisted("The application link leaves the approved employer origin.")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=16000)
        except Exception as error:
            if type(error).__module__.startswith("playwright"):
                raise Assisted("The employer page could not be reached within the browser guard.") from None
            raise
        if not response:
            raise Assisted("No application response was received.")
        destination = gate.redirects.pop(response.url, None)
        if not destination:
            check_access(page, gate, response)
            return
        url = destination
    raise Assisted("The employer redirected too many times.")


def links(page, label):
    candidates = []
    for link in page.get_by_role("link", name=label).all()[:20]:
        if link.is_visible() and (href := link.get_attribute("href")):
            candidates.append(urljoin(page.url, href))
    return list(dict.fromkeys(candidates))


def navigation_choices(page, label):
    candidates = links(page, label)
    for form in page.locator("form").all()[:20]:
        if not form.get_by_role("button", name=label).count():
            continue
        detail = form.evaluate(FORM_SNAPSHOT)
        active = [f for f in detail["fields"] if not f["disabled"] and
                  f["type"] not in {"submit", "button", "reset"}]
        if detail["method"] == "get" and not detail["unsafe"] and not active:
            candidates.append(detail["action"])
    return list(dict.fromkeys(candidates))


def candidate_forms(page):
    for frame in page.frames:
        for form in frame.locator("form").all()[:20]:
            if not form.is_visible():
                continue
            details = form.evaluate(FORM_SNAPSHOT)
            fields = details["fields"]
            has_file = any(f["type"] == "file" and not f["disabled"] for f in fields)
            has_email = any(f["type"] == "email" or f["name"].casefold() == "email"
                            or " ".join(f["label"].casefold().strip(" *:").split()) in {"email", "email address"}
                            for f in fields if not f["disabled"])
            if has_file and has_email and form.locator(SUBMIT).count() == 1:
                yield form, details


def find_form(page, gate, target, privacy=False):
    urls, moves, apply_moves = {target}, [], 0
    open_page(page, gate, target)
    for _ in range(4):
        forms = list(candidate_forms(page))
        if len(forms) > 1:
            raise Assisted("More than one application form matches this page.")
        if forms:
            form, details = forms[0]
            if (details["unsafe"] or details["method"] != "post" or not gate.allows(details["action"])
                    or details["encoding"] != "multipart/form-data"):
                raise Assisted("The form uses an unfamiliar submission destination or encoding.")
            consent = []
            for field in details["fields"]:
                label = field.get("group") or field["label"]
                normalized = " ".join(label.strip(" .:*").split())
                if (privacy and field["type"] == "checkbox" and PRIVACY.fullmatch(normalized)
                        and not field.get("group")):
                    field["approved_consent"] = True
                    consent.append(label)
            body = details["text"]
            for label in consent:
                body = body.replace(label, "")
            if LEGAL_TEXT.search(body):
                raise Assisted("The form contains a legal attestation or unapproved consent.")
            return form, details, "/".join(moves) or "direct"
        apply_urls = navigation_choices(page, APPLY) if not apply_moves else []
        if apply_urls:
            destinations, move = apply_urls, "apply"
            apply_moves += 1
        else:
            destinations, move = navigation_choices(page, NEXT), "next"
        if len(destinations) != 1 or destinations[0] in urls:
            break
        urls.add(destinations[0])
        moves.append(move)
        open_page(page, gate, destinations[0])
    raise Assisted("No unique standard application form was found within the navigation limit.")


def submission_snapshot(form, details, resume_version):
    payload = []
    for name, value, hidden in form.evaluate(BODY_FIELDS, SUBMIT):
        if hidden or SECRET.search(name):
            value = hidden_value(name, value)
        elif isinstance(value, dict):
            value = {"file": value["file"]}
        payload.append([name, value])
    return {"action": details["action"], "fields": payload, "resume_version": resume_version,
            "consents": [f["label"] for f in details["fields"] if f.get("approved_consent")]}


def hidden_value(name, value):
    redacted = {"redacted": True}
    if POSTING_FIELD.search(name) and not SECRET.search(name) and isinstance(value, str):
        redacted["fingerprint"] = hashlib.sha256(value.encode()).hexdigest()
    return redacted


def posted_snapshot(request, details, resume_version):
    content_type = request.header_value("content-type") or ""
    payload = request.post_data_buffer
    if not content_type.startswith("multipart/form-data;") or not payload or len(payload) > 10_000_000:
        raise NeedsReview("The manual form payload could not be recorded safely.")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + payload)
    if not message.is_multipart() or message.defects:
        raise NeedsReview("The manual form payload could not be recorded safely.")
    hidden = {field["name"] for field in details["fields"] if field["type"] == "hidden"}
    fields = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name or part.defects:
            raise NeedsReview("The manual form includes an unfamiliar submission field.")
        if name in hidden or SECRET.search(name):
            value = hidden_value(name, part.get_content())
        elif part.get_filename():
            value = {"file": part.get_filename()}
        else:
            value = part.get_content()
        fields.append([name, value])
    return {"action": request.url, "fields": fields, "resume_version": resume_version, "consents": []}


def submit_form(page, gate, form, details, snapshot, before):
    submit = form.locator(SUBMIT)
    if submit.count() != 1 or not submit.is_visible() or not submit.is_enabled() or not form.evaluate("f => f.checkValidity()"):
        raise Assisted("The employer form has unanswered fields or an unavailable submit button.")
    if submit.evaluate("""el => ['formaction','formmethod','formenctype','formtarget'].some(x=>el.hasAttribute(x))"""):
        raise Assisted("The submit control changes the form destination.")
    old_url = page.url
    old_body = page.locator("body").inner_text(timeout=5000)
    before(details["action"], snapshot)
    gate.endpoint = details["action"]
    submit.click(timeout=15000)
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    return verify_page(page, gate, old_url, old_body)


def verify_page(page, gate, old_url, old_body):
    try:
        destination = gate.redirects.pop(gate.endpoint, None)
        if destination:
            open_page(page, gate, destination)
        else:
            check_access(page, gate)
    except NeedsReview:
        return {"state": "uncertain", "confirmation": None, "url": old_url}
    body = page.locator("body").inner_text(timeout=5000)
    phrase = CONFIRMATION.search(body)
    if (not gate.sent or gate.blocked_post or gate.status not in {*range(200, 300), 302, 303}
            or not phrase or CONFIRMATION.search(old_body) or (old_url == page.url and old_body == body)
            or list(candidate_forms(page))):
        return {"state": "uncertain", "confirmation": None, "url": old_url}
    url = urlsplit(page.url)._replace(query="", fragment="").geturl()
    return {"state": "probable", "confirmation": phrase[0], "url": url}
