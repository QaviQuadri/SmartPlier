import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.check_no_secrets import scan


class ScannerTests(unittest.TestCase):
    def test_findings_never_return_secret_values(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.example.json"
            name = "Unique " + "Canarycandidate"
            reference.write_text(json.dumps({"basics": {"name": name}}), encoding="utf-8")
            material = root / "package"
            material.mkdir()
            key = "AI" + "za" + "Z" * 35
            password = "azby" * 4
            private = material / "fixture.html"
            private.write_text(f"{name}\n{key}\nGMAIL_APP_PASSWORD={password}\n", encoding="utf-8")
            result = list(scan(material, reference))
            self.assertEqual(result, [("fixture.html", 1, "Original profile identity"),
                                      ("fixture.html", 2, "API key"),
                                      ("fixture.html", 3, "Gmail app password")])
            self.assertNotIn(password, str(result))

    def test_flags_database_and_ignored_profile(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "renamed.bin").write_bytes(b"SQLite format 3\x00")
            (root / "profile.json").write_text("{}", encoding="utf-8")
            self.assertEqual(list(scan(root)), [("profile.json", 0, "Local configuration"),
                                               ("renamed.bin", 0, "Local database")])
