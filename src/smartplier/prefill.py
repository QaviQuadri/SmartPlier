"""Review basic contact fields on simple Lever-hosted application forms."""

import re
import sys
from urllib.parse import urlsplit


HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")
POSTING = re.compile(r"/([a-zA-Z0-9_-]{1,120})/([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?:/apply)?/?")
FIELDS = (
    ("name", "Name", 'input[name="name"]:is([type="text"], :not([type]))'),
    ("email", "Email", 'input[name="email"]:is([type="email"], [type="text"], :not([type]))'),
    ("phone", "Phone", 'input[name="phone"]:is([type="tel"], [type="text"], :not([type]))'),
    ("linkedin", "LinkedIn", 'input[name="urls[LinkedIn]" i]:is([type="url"], [type="text"], :not([type]))'),
    ("github", "GitHub", 'input[name="urls[GitHub]" i]:is([type="url"], [type="text"], :not([type]))'),
    ("portfolio", "Portfolio", 'input[name="urls[Portfolio]" i]:is([type="url"], [type="text"], :not([type]))'),
)


class PrefillError(ValueError):
    """An ATS prefill error that contains no applicant details."""


def lever_apply_url(value):
    if "\\" in value or any(char.isspace() or not char.isprintable() for char in value):
        raise PrefillError("Prefill needs a standard HTTPS Lever job link.")
    try:
        url = urlsplit(value)
        path = POSTING.fullmatch(url.path)
        valid = (url.scheme == "https" and url.hostname in HOSTS and url.port in (None, 443)
                 and url.username is None and url.password is None and path)
    except ValueError:
        valid = False
    if not valid:
        raise PrefillError("Prefill supports standard Lever job links on jobs.lever.co and jobs.eu.lever.co.")
    return f"https://{url.hostname}/{path[1]}/{path[2]}/apply"


def route_request(route, host):
    request = route.request
    try:
        target = urlsplit(request.url)
        allowed = (request.method in ("GET", "HEAD") and target.scheme == "https" and target.hostname == host
                   and target.port in (None, 443) and target.username is None and target.password is None)
    except ValueError:
        allowed = False
    if not allowed:
        route.abort()
        return
    # Fetch a single hop ourselves so the browser never follows redirects.
    response = route.fetch(max_redirects=0)
    if 300 <= response.status < 400:
        route.abort()
    else:
        route.fulfill(response=response)


def contact_fields(page, basics):
    forms = page.locator("form").filter(has=page.locator('input[name="name"]')).filter(
        has=page.locator('input[name="email"]')
    )
    if forms.count() != 1:
        raise PrefillError("No unique Lever form with basic contact fields was found. Open the posting manually.")
    filled = []
    for key, label, selector in FIELDS:
        value = basics.get(key)
        field = forms.locator(selector)
        if not value or field.count() != 1 or not field.is_visible() or not field.is_editable() or field.input_value():
            continue
        value = value.strip()
        if key in ("linkedin", "github", "portfolio") and not value.startswith(("https://", "http://")):
            value = "https://" + value
        field.fill(value)
        filled.append(label)
    return filled


def review_prefill(url, profile):
    target = lever_apply_url(url)
    if not sys.stdin.isatty():
        raise PrefillError("Run prefill in an interactive terminal so you can review the form.")
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise PrefillError("Install optional Playwright with python -m pip install playwright, then run "
                           "python -m playwright install chromium.") from None

    profile_fields = profile["basics"]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block", accept_downloads=False)
                online = True
                host = urlsplit(target).hostname

                def guard(route):
                    if online:
                        route_request(route, host)
                    else:
                        route.abort()

                context.route("**/*", guard)
                page = context.new_page()
                response = page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                if response is None or not response.ok or lever_apply_url(page.url) != target:
                    raise PrefillError("The expected Lever application page could not be opened.")
                # Leave the browser offline before entering any personal data.
                online = False
                context.set_offline(True)
                filled = contact_fields(page, profile_fields)
                if not filled:
                    raise PrefillError("No empty, editable contact fields were available.")
                print("Prefilled for review: " + ", ".join(filled) + ".")
                print("This preview is offline. Review in the browser and press Enter here to close it.")
                input()
            finally:
                browser.close()
    except Error:
        raise PrefillError("The browser or Lever form could not be opened safely. Check that Chromium is installed.") from None
    except (KeyboardInterrupt, EOFError):
        return
