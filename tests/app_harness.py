"""
Shared harness for the end-to-end app tests: copies app.py into a temporary
folder with sample data (your real runs/ is never touched), starts it, and
signs in. Each start_app() call is like a server restart.

Needs the app's own dependencies (flask, opencv, ...); tests using
AppTestCase are skipped without them.
"""

import contextlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import db
from tests.sample_data import write_sample

try:
    import cv2  # noqa: F401
    import flask  # noqa: F401
    from werkzeug.security import generate_password_hash
    HAVE_APP_DEPS = True
except ImportError:
    HAVE_APP_DEPS = False

REPO = Path(__file__).resolve().parent.parent
APP_FILES = ["app.py", "bar_ocr.py", "db.py", "db_import.py", "manage_users.py"]


@unittest.skipUnless(HAVE_APP_DEPS, "app dependencies (flask, opencv-python) not installed")
class AppTestCase(unittest.TestCase):
    _instances = 0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in APP_FILES:
            shutil.copy(REPO / name, self.root / name)
        for folder in ("templates", "static"):
            shutil.copytree(REPO / folder, self.root / folder)

        self.runs = self.root / "runs"
        write_sample(self.runs, n_videos=80)
        # Every sample job finished, so the app's worker doesn't try to run SpeciesNet.
        jobs = json.loads((self.runs / "jobs_index.json").read_text())
        for j in jobs.values():
            j["status"] = "done"
        (self.runs / "jobs_index.json").write_text(json.dumps(jobs))
        # A real password, so the tests can sign in.
        (self.runs / "users.json").write_text(json.dumps({
            "tester": {"password_hash": generate_password_hash("pw"), "first_name": "Test", "last_name": "User"},
        }))
        self.original = json.loads((self.runs / "videos_index.json").read_text())
        self._orig_db_path = db.DB_PATH

    def tearDown(self):
        db.close()
        db.DB_PATH = self._orig_db_path
        self._tmp.cleanup()

    def start_app(self):
        """Imports a fresh copy of app.py — each call is like a server restart."""
        AppTestCase._instances += 1
        name = f"app_under_test_{AppTestCase._instances}"
        spec = importlib.util.spec_from_file_location(name, self.root / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        client = module.app.test_client()
        r = client.post("/api/auth/login", json={"username": "tester", "password": "pw"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return module, client

    def assert_same_records(self, label, stored, in_memory):
        """Same keys, and every record identical field for field (diffs named on failure)."""
        self.assertEqual(set(stored), set(in_memory), f"{label}: different keys")
        for key, mem in in_memory.items():
            diffs = {
                k: {"db": stored[key].get(k, "<missing>"), "app": mem.get(k, "<missing>")}
                for k in set(mem) | set(stored[key])
                if stored[key].get(k, "<missing>") != mem.get(k, "<missing>")
            }
            self.assertEqual(diffs, {}, f"{label} {key} differs")

    def assert_db_matches(self, app_module):
        """The database holds exactly the app's in-memory data, for every kind of data."""
        db.close()  # read through a fresh connection, like a new process would
        self.assert_same_records("video", db.load_all("videos"), app_module.videos)
        self.assert_same_records("job", db.load_all("jobs"), app_module.jobs)
        self.assert_same_records("upload batch", app_module._load_upload_batches(), app_module.upload_batches)
        self.assertEqual(
            {n: {"lat": r["lat"], "lon": r["lon"]} for n, r in db.load_all("locations").items()},
            app_module.locations,
        )
        self.assertEqual(sorted(db.load_all("species")), app_module.canonical_species)
        self.assertEqual(
            db.load_all("categories"),
            {label: app_module._category_row(label, cfg) for label, cfg in app_module.category_settings.items()},
        )
        stored_storage = db.get_setting("storage")
        if stored_storage is not None:  # None = never changed, app uses its built-in defaults
            self.assertEqual(stored_storage, app_module.storage_settings)
        self.assertEqual(
            {name: {k: v for k, v in r.items() if k != "name"} for name, r in db.load_all("ocr_configs").items()},
            app_module.ocr_configs,
        )
        self.assertEqual(db.get_setting("ocr_last_used"), app_module.ocr_last_used)
        self.assertEqual(db.get_setting("ocr_disabled"), app_module.ocr_disabled)

    @staticmethod
    @contextlib.contextmanager
    def fake_speciesnet():
        """
        While active, any job the app starts runs a do-nothing command that
        exits successfully instead of MegaDetector/SpeciesNet. On a machine
        where those are installed (the real server), tests would otherwise
        start real GPU runs.
        """
        real_popen = subprocess.Popen

        def popen(cmd, *args, **kwargs):
            if any("speciesnet" in str(part) or "megadetector" in str(part) for part in cmd):
                cmd = [sys.executable, "-c", "pass"]
            return real_popen(cmd, *args, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=popen):
            yield

    def wait_for_jobs_idle(self, app_module, timeout=60):
        """Waits until the app's worker has nothing queued or running."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app_module.jobs_lock:
                busy = [j for j in app_module.jobs.values() if j["status"] in ("queued", "running", "cancelling")]
            if not busy:
                return
            time.sleep(0.1)
        self.fail(f"jobs still busy after {timeout}s: {busy}")

