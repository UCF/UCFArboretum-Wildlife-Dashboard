"""
End-to-end tests of the last data moved to SQLite: users (and
manage_users.py), OCR presets, species, category settings, storage
settings and the one-time migrations list. Drives the real app in a
temporary copy (see app_harness).

    python -m unittest tests.test_app_settings_users -v
"""

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from unittest import mock

import db
import db_import
from tests.app_harness import AppTestCase


class AppSettingsUsersTests(AppTestCase):
    def manage_users(self, *argv, password="s3cret"):
        """Runs the temp copy of manage_users.py in-process, typing `password` at its prompts."""
        spec = importlib.util.spec_from_file_location("manage_users_under_test", self.root / "manage_users.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.StringIO()
        with mock.patch("getpass.getpass", return_value=password), contextlib.redirect_stdout(out):
            module.COMMANDS[argv[0]](list(argv[1:]))
        db.close()
        return out.getvalue()

    # ------------------------------------------------------------------

    def test_first_start_imports_every_json_file(self):
        app_module, _ = self.start_app()
        leftovers = sorted(p.name for p in self.runs.glob("*.json"))
        self.assertEqual(leftovers, [])
        self.assertEqual(sorted(p.name[:-4] for p in self.runs.glob("*.json.bak")), sorted(db_import.SOURCES))

        sample_ocr = json.loads((self.runs / "ocr_configs.json.bak").read_text())
        self.assertEqual(app_module.ocr_configs, sample_ocr["configs"])
        self.assertEqual(app_module.ocr_last_used, "Browning 1080p")
        self.assertEqual(app_module.storage_settings,
                         json.loads((self.runs / "storage_settings.json.bak").read_text()))
        self.assertEqual(app_module.canonical_species,
                         json.loads((self.runs / "species_list.json.bak").read_text()))
        self.assert_db_matches(app_module)

    def test_settings_edits_survive_restart(self):
        app_module, client = self.start_app()
        raccoon = "mammalia;carnivora;procyonidae;procyon;lotor;common raccoon"

        steps = [
            ("/api/ocr-configs", {"name": "Bushnell 4K", "bar_box": [0, 2000, 3840, 2160],
                                  "ref_width": 3840, "ref_height": 2160}),
            ("/api/ocr-configs/disabled", {"disabled": True}),
            ("/api/categories/private", {"label": raccoon, "private": True}),
            ("/api/storage/auto-empty", {"enabled": False, "threshold": 75}),
        ]
        for url, body in steps:
            r = client.post(url, json=body)
            self.assertEqual(r.status_code, 200, f"{url}: {r.get_data(as_text=True)}")
            self.assert_db_matches(app_module)
        r = client.delete("/api/ocr-configs/Browning 1080p")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_db_matches(app_module)

        restarted, _ = self.start_app()
        self.assertEqual(set(restarted.ocr_configs), {"Bushnell 4K"})
        self.assertEqual(restarted.ocr_configs["Bushnell 4K"]["bar_box"], [0, 2000, 3840, 2160])
        self.assertEqual(restarted.ocr_last_used, "Bushnell 4K")
        self.assertIs(restarted.ocr_disabled, True)
        self.assertIs(restarted.category_settings[raccoon]["private"], True)
        self.assertEqual(restarted.storage_settings["auto_empty_threshold"], 75)
        self.assertIs(restarted.storage_settings["auto_empty_enabled"], False)

    def test_fresh_install_without_any_json(self):
        for name in db_import.SOURCES:
            (self.runs / name).unlink()
        app_module, _ = self.start_app_without_login()
        self.assertEqual(app_module.videos, {})
        self.assertEqual(app_module.ocr_configs, {})
        self.assertEqual(app_module.ocr_last_used, app_module.SKIP_OCR_VALUE)
        self.assertIs(app_module.ocr_disabled, False)
        self.assertEqual(app_module.storage_settings["auto_empty_threshold"], 90)  # built-in default
        self.assert_db_matches(app_module)

    def test_blank_count_migration_runs_once(self):
        # An install that never ran the blank-count fix, with one affected entry.
        (self.runs / "migrations.json").write_text("[]")
        videos = json.loads((self.runs / "videos_index.json").read_text())
        blank_id = next(v for v in videos.values() if not v["ai_species"] and not v["corrected_species"])["id"]
        videos[blank_id]["count"] = 1
        (self.runs / "videos_index.json").write_text(json.dumps(videos))

        app_module, client = self.start_app()
        self.assertEqual(app_module.videos[blank_id]["count"], 0)
        self.assertIn("blank_count_zero", db.applied_migrations())
        self.assert_db_matches(app_module)

        # A person later sets the count back to 1; a restart must not "fix" it again.
        client.post(f"/api/videos/{blank_id}/update", json={"count": 1})
        restarted, _ = self.start_app()
        self.assertEqual(restarted.videos[blank_id]["count"], 1)

    def test_manage_users_while_server_runs(self):
        app_module, client = self.start_app()  # signed in as the imported "tester"

        out = self.manage_users("add", "jdoe", "Jamie", "Doe")
        self.assertIn("Added user 'jdoe'", out)
        other = app_module.app.test_client()
        r = other.post("/api/auth/login", json={"username": "jdoe", "password": "s3cret"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))  # no restart needed
        r = other.post("/api/auth/login", json={"username": "jdoe", "password": "wrong"})
        self.assertEqual(r.status_code, 401)

        self.assertIn("jdoe: Jamie Doe", self.manage_users("list"))

        self.manage_users("remove", "tester")
        self.assertIs(client.get("/api/auth/me").get_json()["signed_in"], False)

    def test_manage_users_before_first_server_start(self):
        # Upgraded code, server not started yet: the old users.json must not be lost.
        self.manage_users("add", "newbie", "New", "Person")
        self.assertFalse((self.runs / "users.json").exists())
        listing = self.manage_users("list")
        self.assertIn("tester: Test User", listing)
        self.assertIn("newbie: New Person", listing)

        app_module, _ = self.start_app()  # still logs in as "tester"
        self.assert_db_matches(app_module)

    # ------------------------------------------------------------------

    def start_app_without_login(self):
        """start_app() for a fresh install, which has no accounts to sign in with."""
        AppTestCase._instances += 1
        name = f"app_under_test_{AppTestCase._instances}"
        spec = importlib.util.spec_from_file_location(name, self.root / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module, module.app.test_client()


if __name__ == "__main__":
    unittest.main()
