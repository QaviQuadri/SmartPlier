import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from smartplier import form_recipes
from smartplier.form_engine import NeedsReview
from tests.test_form_engine import control
from smartplier.tracker import database
from smartplier.web_service import prepare


class RecipeTests(unittest.TestCase):
    def test_fingerprint_ignores_case_whitespace_ids_and_values(self):
        first = control("one", "  Work   email ", "email", value="secret")
        second = control("two", "work email?", "email", value="different")
        self.assertEqual(form_recipes.digest([first]), form_recipes.digest([second]))
        self.assertNotEqual(form_recipes.digest([first]), form_recipes.digest([dict(second, required=False)]))
        self.assertNotEqual(form_recipes.digest([first]), form_recipes.digest([dict(second, type="text")]))

    def test_recipe_reused_and_replanned_on_change(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "recipes.db"
            prepare(path)
            provider = Mock()
            provider.generate_json.return_value = {"fields": [{"id": "0", "key": "email"}]}
            work = SimpleNamespace(provider=provider, connection=lambda: database(path))
            profile = {"basics": {"email": "private@example.test", "name": "Private Candidate"}}
            first = [control("one", "Work email", "email")]
            fingerprint, first_mapping = form_recipes.plan(work, profile, first, "apply", "native-post")
            renamed = [control("two", " work EMAIL ", "email")]
            self.assertEqual(form_recipes.plan(work, profile, renamed, "direct", "native-post"),
                             (fingerprint, first_mapping))
            self.assertEqual(provider.generate_json.call_count, 1)
            changed = [control("one", "Work email", "email", required=False)]
            self.assertNotEqual(form_recipes.plan(work, profile, changed, "apply", "native-post")[0], fingerprint)
            self.assertEqual(provider.generate_json.call_count, 2)
            self.assertNotIn("private@example.test", provider.generate_json.call_args.args[1])
            with database(path) as db, db:
                form_recipes.outcome(db, fingerprint, False)
                form_recipes.outcome(db, fingerprint, False)
            with self.assertRaisesRegex(NeedsReview, "demoted"):
                form_recipes.plan(work, profile, first, "direct", "native-post")

    def test_invalid_output_cannot_be_cached(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "recipes.db"
            prepare(path)
            work = SimpleNamespace(provider=Mock(), connection=lambda: database(path))
            work.provider.generate_json.return_value = {"fields": [{"id": "0", "key": "invented"}]}
            with self.assertRaisesRegex(NeedsReview, "invalid"):
                form_recipes.plan(work, {}, [control("one", "Work email", "email")], "direct", "native-post")
            with database(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM form_recipes").fetchone()[0], 0)
