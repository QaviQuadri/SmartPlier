import unittest
from unittest.mock import Mock

from smartplier.ats_detection import detect
from smartplier.form_engine import NeedsReview, fill_fields, inspect_generic, inspect_summary


def control(ident, label, kind="text", **attributes):
    return {"name": ident, "id": ident, "label": label, "type": kind, "visible": True,
            "required": True, "disabled": False, **attributes}


def employer_form():
    form = Mock()
    locators = {}

    def locate(selector):
        if selector not in locators:
            locators[selector] = Mock()
            locators[selector].count.return_value = 1
        return locators[selector]

    form.locator.side_effect = locate
    return form, locators


class DetectionTests(unittest.TestCase):
    def test_platform_comes_from_host_and_posting_path(self):
        urls = {
            "Lever": "https://jobs.lever.co/company/11111111-2222-3333-4444-555555555555",
            "Workable": "https://apply.workable.com/company/j/ABCDE123/",
            "Greenhouse": "https://boards.greenhouse.io/embed/job_app?token=12345",
            "Ashby": "https://jobs.ashbyhq.com/company/11111111-2222-3333-4444-555555555555",
            "SmartRecruiters": "https://jobs.smartrecruiters.com/company/12345-job",
        }
        for name, url in urls.items():
            with self.subTest(name=name):
                result = detect(url)
                self.assertEqual((result.platform, result.confidence), (name, "high"))
                self.assertEqual(result.handler, "generic-inspector")
                self.assertEqual(result.verification, "unverified")

    def test_observed_destination_and_conflicting_platforms(self):
        workable = "https://apply.workable.com/company/j/ABCDE123/apply/"
        result = detect("https://employer.example/careers", observed_url=workable, official_listing=True)
        self.assertEqual((result.platform, result.final_url), ("Workable", workable))
        self.assertIn("official listing and page verified", result.verification)
        self.assertTrue(any("Observed navigation" in note for note in result.evidence))
        result = detect("https://job-boards.greenhouse.io/company/jobs/1234", observed_url=workable)
        self.assertIsNone(result.handler)
        self.assertIsNotNone(result.ambiguity)

    def test_unknown_domains_do_not_establish_a_submission_handler(self):
        url = "https://jobs.lever.co.example.org/company/anything"
        self.assertIsNone(detect(url).handler)
        self.assertEqual(detect(url, form_inspected=True).handler, "generic-inspector")
        self.assertIsNone(detect("https://jobs.lever.co@localhost/example").handler)
        self.assertIsNotNone(detect("https://apply.workable.com/company").ambiguity)


class FormResolverTests(unittest.TestCase):
    def test_maps_basic_fields_and_exact_verified_choices(self):
        form, locators = employer_form()
        options = [{"label": label, "disabled": False} for label in ("Python", "SQL", "Other")]
        fields = [
            control("contact", "Email address", "email"),
            control("tech", "Select your tools", "select-multiple", options=options),
            control("start", "Earliest start", "date"),
            control("yes", "Yes", "radio", name="travel", group="Can you travel?", option_label="Yes"),
            control("no", "No", "radio", name="travel", group="Can you travel?", option_label="No"),
            control("resume", "Resume", "file", accept=".pdf"),
        ]
        basics = {"email": "candidate@example.test"}
        profile = {"verified_screening_answers": {
            "Select your tools": ["Python", "SQL"], "Earliest start": "2026-12-01", "Can you travel?": "Yes",
        }}
        summary = inspect_summary(form, fields, basics, profile)
        self.assertEqual(summary["unanswered"], [])
        self.assertNotIn(basics["email"], str(summary))
        fill_fields(form, fields, basics, profile, "tailored.pdf")
        locators['[id="contact"]'].fill.assert_called_once_with(basics["email"], timeout=1500)
        locators['[id="tech"]'].select_option.assert_called_once_with(label=["Python", "SQL"], timeout=1500)
        locators['[id="start"]'].fill.assert_called_once_with("2026-12-01", timeout=1500)
        locators['[id="yes"]'].check.assert_called_once_with(timeout=1500)
        locators['[id="resume"]'].set_input_files.assert_called_once_with("tailored.pdf")

    def test_conflicting_labels_and_attestations_stop_all_fills(self):
        form, locators = employer_form()
        fields = [
            control("email", "Referee email", "email"),
            control("visa", "Do you need sponsorship?", "checkbox"),
            control("resume", "Resume", "file"),
        ]
        profile = {"verified_screening_answers": {"Do you need sponsorship?": "Yes"}}
        with self.assertRaisesRegex(NeedsReview, "referee email, do you need sponsorship"):
            fill_fields(form, fields, {"email": "candidate@example.test"}, profile, "tailored.pdf")
        locators['[id="email"]'].fill.assert_not_called()
        locators['[id="resume"]'].set_input_files.assert_not_called()

    def test_generic_ignores_cross_host_frames_and_requires_a_unique_form(self):
        allowed = Mock(url="https://apply.workable.com/example/j/ABCDE/apply/")
        foreign = Mock(url="https://other.example/form")
        expected = Mock()
        snapshot = {"fields": [control("mail", "Email", "email"), control("resume", "Resume", "file")]}
        expected.evaluate.return_value = snapshot
        allowed.locator.return_value.all.return_value = [expected]
        page = Mock(frames=[allowed, foreign])
        self.assertEqual(inspect_generic(page, "apply.workable.com"), (expected, snapshot))
        foreign.locator.assert_not_called()
        allowed.locator.return_value.all.return_value = [expected, expected]
        with self.assertRaisesRegex(NeedsReview, "No unique application form"):
            inspect_generic(page, "apply.workable.com")
