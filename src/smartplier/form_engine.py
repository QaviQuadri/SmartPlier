"""Shared browser form inspection and deterministic profile answer planning."""

import re
from datetime import date


class NeedsReview(ValueError):
    pass


class PolicyBlocked(NeedsReview):
    pass


def fields_from_profile(basics, profile):
    education = profile.get("education", [])
    experience = profile.get("experience", [])
    degree = education[0] if len(education) == 1 else {}
    links = {key: ("https://" + basics[key] if "://" not in basics[key] else basics[key])
             for key in ("linkedin", "github", "portfolio") if basics.get(key)}
    return {
        "name": basics.get("name"), "full name": basics.get("name"),
        "first name": basics.get("first_name"), "last name": basics.get("last_name"),
        "email": basics.get("email"), "email address": basics.get("email"),
        "phone": basics.get("phone"), "phone number": basics.get("phone"),
        "location": basics.get("location"), "current location": basics.get("location"),
        "urls[linkedin]": links.get("linkedin"), "linkedin profile": links.get("linkedin"),
        "linkedin": links.get("linkedin"), "linkedin url": links.get("linkedin"),
        "urls[github]": links.get("github"), "github": links.get("github"), "github url": links.get("github"),
        "urls[portfolio]": links.get("portfolio"), "portfolio": links.get("portfolio"), "portfolio url": links.get("portfolio"),
        "university": degree.get("school"), "school": degree.get("school"), "degree": degree.get("degree"),
        "gpa": degree.get("gpa"),
        "skills": ", ".join(profile.get("skills", [])),
        "work experience": "\n".join(f"{e['title']} at {e['company']} ({e['start']} – {e['end']})" for e in experience),
    }


def fill_fields(form, fields, basics, profile, pdf, screening=None):
    fills, unknown = field_plan(form, fields, basics, profile, pdf, screening)
    if unknown:
        raise NeedsReview("Unverified or protected fields: " + ", ".join(unknown[:5]))
    apply_fields(fills)


def apply_fields(fills):
    for locator, kind, value, _ in fills:
        if kind == "file":
            locator.set_input_files(value)
        elif kind == "select-one":
            locator.select_option(label=value, timeout=1500)
        elif kind == "select-multiple":
            locator.select_option(label=value, timeout=1500)
        elif kind in {"radio", "checkbox"}:
            locator.check(timeout=1500)
        else:
            locator.fill(value, timeout=1500)


def field_plan(form, fields, basics, profile, pdf, screening=None):
    answers = fields_from_profile(basics, profile)
    verified = {normalize(k): v for k, v in profile.get("verified_screening_answers", {}).items()
                if isinstance(v, str) and v.strip() or isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v)}
    fills, unknown = [], []
    seen_radio = set()
    for item in fields:
        kind, name = item["type"], item["name"]
        label = normalize(item.get("group") or item["label"])
        display = item.get("group") or item["label"] or name
        resume = kind == "file" and (item.get("recipe_key") == "resume" or normalize(name) == "resume"
                                    or label in {"resume", "resume/cv", "cv", "upload resume"})
        if item["disabled"] or kind in {"hidden", "submit", "button", "reset"}:
            continue
        if not item["visible"] and not resume:
            if item["required"]:
                unknown.append("Hidden mandatory field")
            continue
        address = item.get("id") or name
        if not re.fullmatch(r"[\w.\[\] -]{1,100}", address):
            if item["required"]:
                unknown.append("Unrecognized mandatory field")
            continue
        legal = re.search(r"visa|sponsor|authoriz|eligib|right to work|citizen|consent|attest|criminal|"
                          r"terms|privacy|disability|race|gender|certify|legal", f"{name} {label}", re.I)
        guarded = bool(legal)
        if screening:
            from smartplier.screening import protection
            guarded = guarded or bool(protection(label, technical_name=name))
        if item.get("approved_consent"):
            guarded = False
        if guarded and not screening and (item["required"] or kind in {"checkbox", "radio", "select-one"}):
            unknown.append(label or name)
            continue
        if guarded and not screening:
            continue
        locator = form.locator(f'[{"id" if item.get("id") else "name"}="{address}"]')
        answer = answers.get(label) if label else answers.get(normalize(name))
        if not screening and not answer:
            answer = verified.get(label)
        required = item["required"] or kind == "radio" and any(
            field["type"] == kind and field["name"] == name and field["required"] for field in fields
        )
        if item.get("approved_consent"):
            answer = "Yes"
        elif screening and not resume and (guarded or not answer and required):
            answer = screening.resolve(item, fields, required)
        if guarded and not answer:
            if required or kind in {"checkbox", "radio", "select-one", "select-multiple"}:
                unknown.append(display)
            continue
        if kind == "radio":
            if name in seen_radio:
                continue
            seen_radio.add(name)
            group = [candidate for candidate in fields if candidate["type"] == "radio" and candidate["name"] == name]
            required = any(candidate["required"] for candidate in group)
            choices = [candidate for candidate in group if candidate["visible"] and not candidate["disabled"]
                       and normalize(candidate.get("option_label", "")) == normalize(answer if isinstance(answer, str) else "")]
            if item.get("group") and answer and len(choices) == 1 and re.fullmatch(r"[\w.\[\] -]{1,100}", choices[0].get("id", "")):
                choice = form.locator(f'[id="{choices[0]["id"]}"]')
                if choice.count() == 1:
                    fills.append((choice, kind, True, display))
                    continue
            if required:
                unknown.append(display)
            continue
        if kind in {"select-one", "select-multiple"} and answer and locator.count() == 1:
            labels = [o["label"].strip() for o in item.get("options", []) if not o["disabled"]]
            values = answer if isinstance(answer, list) else [answer]
            if not all(labels.count(value) == 1 for value in values) or kind == "select-one" and len(values) != 1:
                answer = None
            else:
                answer = values if kind == "select-multiple" else values[0]
        if kind == "date" and answer:
            try:
                answer = date.fromisoformat(answer).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer) else None
            except (ValueError, TypeError):
                answer = None
        if kind == "checkbox" and (item.get("group") or normalize(answer if isinstance(answer, str) else "") != "yes"):
            answer = None
        if kind != "select-multiple" and not isinstance(answer, str):
            answer = None
        accept = {part.strip().casefold() for part in item.get("accept", "").split(",") if part.strip()}
        if not item.get("readonly") and locator.count() == 1 and resume and (not accept or accept & {".pdf", "application/pdf", "*/*"}):
            fills.append((locator, "file", pdf, display))
        elif not item.get("readonly") and locator.count() == 1 and answer and kind in {"text", "email", "tel", "url", "textarea", "number", "select-one", "select-multiple", "date", "checkbox"}:
            fills.append((locator, kind, answer, display))
        elif item["required"]:
            unknown.append(label or name)
    if not any(kind == "file" for _, kind, _, _ in fills):
        unknown.append("No supported resume upload field")
    return fills, unknown


FORM_SNAPSHOT = r"""f => {
  const text = e => (e?.innerText || e?.textContent || '').trim();
  const labelText = e => {
    const label = e.cloneNode(true);
    label.querySelectorAll('input,select,textarea,button').forEach(control => control.remove());
    return text(label);
  };
  const labelledBy = e => (e.getAttribute('aria-labelledby') || '').split(/\s+/)
    .map(id => text(e.ownerDocument.getElementById(id))).filter(Boolean).join(' ');
  const visible = e => !!e.getClientRects().length &&
    e.ownerDocument.defaultView.getComputedStyle(e).visibility !== 'hidden';
  const controls = [...f.elements].filter(e => ['INPUT','SELECT','TEXTAREA'].includes(e.tagName));
  return {
    action:f.action, method:f.method, encoding:f.enctype, text:f.innerText,
    unsafe: !!(f.target && f.target !== '_self') ||
      f.querySelector('[formaction],[formmethod],[formenctype],[formtarget]') !== null,
    fields: controls.map(e => {
      const label = [...(e.labels || [])].map(labelText).filter(Boolean).join(' ');
      const group = e.type === 'radio' || e.type === 'checkbox' ? text(e.closest('fieldset')?.querySelector('legend')) : '';
      return {name:e.name, id:e.id, type:e.type, required:e.required || e.getAttribute('aria-required') === 'true',
        disabled:e.matches(':disabled'), readonly:e.readOnly || e.getAttribute('aria-readonly') === 'true',
        visible:visible(e), label:(labelledBy(e) || e.getAttribute('aria-label') || label || e.getAttribute('placeholder') || '').slice(0,300),
        option_label:label, group, accept:e.accept || '', min:e.min || '', max:e.max || '',
        validation:(e.validationMessage || '').slice(0,300),
        options:[...(e.options || [])].map(o=>({label:o.label,value:o.value,disabled:o.matches(':disabled')}))};
    })
  };
}"""


def inspect_generic(page, approved_host):
    """Describe a uniquely identifiable form in the page or an approved same-host frame."""
    from urllib.parse import urlsplit

    candidates = []
    for frame in page.frames:
        origin = urlsplit(frame.url)
        if origin.scheme != "https" or origin.hostname != approved_host:
            continue
        forms = frame.locator("form")
        for form in forms.all()[:20]:
            if not form.is_visible():
                continue
            snapshot = form.evaluate(FORM_SNAPSHOT)
            fields = snapshot["fields"]
            email = any(field["type"] == "email" or normalize(field["label"]) in {"email", "email address"} for field in fields)
            if email and any(field["type"] == "file" or field["required"] for field in fields):
                candidates.append((form, snapshot))
    if len(candidates) != 1:
        raise NeedsReview("No unique application form could be identified on the approved pages.")
    return candidates[0]


def normalize(text):
    return " ".join(text.casefold().strip(" *:").split())


def inspect_summary(form, fields, basics, profile, screening=None):
    fills, unanswered = field_plan(form, fields, basics, profile, None, screening)
    return {
        "fields": [label for _, kind, _, label in fills if kind != "file"],
        "unanswered": unanswered,
        "controls": [{key: item.get(key) for key in ("type", "label", "required", "validation", "options")}
                     for item in fields if item["visible"] and not item["disabled"] and item["type"] != "hidden"],
    }
