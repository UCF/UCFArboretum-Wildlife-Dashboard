"""
Tests for db_import.py, run against sample runs/ folders built in a temp
directory. Run from the repo root with:

    python -m unittest discover tests -v
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import db
import db_import
from tests.sample_data import make_sample, write_sample


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name) / "runs"
        write_sample(self.runs, n_videos=120)
        self.sample = make_sample(n_videos=120)
        self._orig_path = db.DB_PATH
        db.DB_PATH = self.runs / "wildlife.db"
        db.init_db()
        self.log = []

    def tearDown(self):
        db.close()
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def run_import(self, files=None, dry_run=False):
        db_import.import_sources(self.runs, files or list(db_import.SOURCES), dry_run=dry_run, log=self.log.append)

    def write_json(self, name, data):
        (self.runs / name).write_text(json.dumps(data))


class FullImportTests(ImportTestCase):
    def test_every_record_arrives_identical(self):
        self.run_import()
        s = self.sample

        self.assertEqual(db.load_all("videos"), s["videos_index.json"])
        self.assertEqual(db.load_all("jobs"), s["jobs_index.json"])
        self.assertEqual(sorted(db.load_all("species")), s["species_list.json"])
        self.assertEqual(
            {n: {"lat": r["lat"], "lon": r["lon"]} for n, r in db.load_all("locations").items()},
            s["locations.json"],
        )

        # Upload batches are split across two tables; rebuild the JSON shape and compare.
        batches = db.load_all("upload_batches")
        for (batch_id, rel), f in db.load_all("upload_batch_files").items():
            batches[batch_id].setdefault("files", {})[rel] = {
                k: f[k] for k in ("size", "expected_chunks", "received_chunks")
            }
        for b in batches.values():
            b.pop("id")
        self.assertEqual(batches, s["upload_batches_index.json"])

        self.assertEqual(db.get_setting("storage"), s["storage_settings.json"])
        self.assertEqual(db.get_setting("ocr_last_used"), "Browning 1080p")
        self.assertIs(db.get_setting("ocr_disabled"), False)
        self.assertEqual(db.get("ocr_configs", "Browning 1080p")["bar_box"], [0, 1012, 1920, 1080])
        self.assertEqual(db.get("users", "arivera")["password_hash"], s["users.json"]["arivera"]["password_hash"])
        self.assertIn("blank_count_zero", db.applied_migrations())

        private = db.get("categories", "reptilia;testudines;testudinidae;gopherus;polyphemus;gopher tortoise")
        self.assertEqual((private["private"], private["cover"], private["cover_version"]), (True, None, 0))

    def test_originals_kept_as_bak_unchanged(self):
        before = {name: (self.runs / name).read_bytes() for name in db_import.SOURCES}
        self.run_import()
        for name, content in before.items():
            self.assertFalse((self.runs / name).exists(), name)
            self.assertEqual((self.runs / f"{name}.bak").read_bytes(), content, name)

    def test_second_run_is_a_no_op(self):
        self.run_import()
        n = db.count("videos")
        self.log.clear()
        self.run_import()
        self.assertEqual(db.count("videos"), n)
        self.assertTrue(all("already imported" in line for line in self.log), self.log)
        self.assertFalse((self.runs / "videos_index.json.bak.1").exists())

    def test_one_file_at_a_time(self):
        # Step-by-step rollout: importing videos must not touch the other files.
        self.run_import(["videos_index.json"])
        self.assertEqual(db.count("videos"), 120)
        self.assertEqual(db.count("jobs"), 0)
        self.assertTrue((self.runs / "jobs_index.json").exists())

    def test_fresh_install_without_files(self):
        for name in db_import.SOURCES:
            (self.runs / name).unlink()
        self.run_import()
        self.assertEqual(db.count("videos"), 0)
        self.assertIn(db_import._marker("videos_index.json"), db.applied_migrations())


class DryRunTests(ImportTestCase):
    def test_dry_run_changes_nothing(self):
        self.run_import(dry_run=True)
        self.assertEqual(db.count("videos"), 0)
        self.assertTrue((self.runs / "videos_index.json").exists())
        self.assertIn("videos_index.json: would import 120 videos", self.log)

    def test_cli_dry_run_creates_no_database(self):
        db.close()
        db.DB_PATH.unlink()
        for extra in ("-wal", "-shm"):
            Path(str(db.DB_PATH) + extra).unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(db_import.main([str(self.runs), "--dry-run"]), 0)
        self.assertIn("would import", out.getvalue())
        self.assertFalse((self.runs / "wildlife.db").exists())


class LegacyDataTests(ImportTestCase):
    def test_old_video_records_get_app_defaults(self):
        # A record from before Date/Time/Count/review-flag existed.
        old_confirmed = {"id": "a1", "job_id": "j", "folder": "/f", "filename": "A.MP4",
                         "ai_species": None, "corrected_species": "blank", "favorited": True}
        old_unconfirmed = {"id": "b2", "job_id": "j", "folder": "/f", "filename": "B.MP4",
                           "ai_species": "x", "notes": None, "count": None}
        self.write_json("videos_index.json", {"a1": old_confirmed, "b2": old_unconfirmed})
        self.run_import(["videos_index.json"])

        a, b = db.get("videos", "a1"), db.get("videos", "b2")
        self.assertIs(a["marked_for_review"], False)   # already confirmed
        self.assertIs(b["marked_for_review"], True)    # still needs review
        self.assertEqual(a["display_filename"], "A.MP4")
        self.assertEqual((b["notes"], b["count"], b["media_type"]), ("", 0, "video"))


class FailureTests(ImportTestCase):
    def assert_untouched(self, name):
        self.assertTrue((self.runs / name).exists())
        self.assertFalse((self.runs / f"{name}.bak").exists())
        self.assertNotIn(db_import._marker(name), db.applied_migrations())

    def test_unknown_field_stops_import(self):
        videos = self.sample["videos_index.json"]
        next(iter(videos.values()))["mystery_field"] = 1
        self.write_json("videos_index.json", videos)
        with self.assertRaisesRegex(db_import.JsonImportError, "mystery_field"):
            self.run_import(["videos_index.json"])
        self.assertEqual(db.count("videos"), 0)
        self.assert_untouched("videos_index.json")

    def test_bad_row_rolls_back_whole_file(self):
        videos = self.sample["videos_index.json"]
        list(videos.values())[-1]["job_id"] = None  # violates NOT NULL, on the LAST row
        self.write_json("videos_index.json", videos)
        with self.assertRaises(db_import.JsonImportError):
            self.run_import(["videos_index.json"])
        self.assertEqual(db.count("videos"), 0)  # the 119 good rows were rolled back too
        self.assert_untouched("videos_index.json")

    def test_corrupt_json(self):
        (self.runs / "jobs_index.json").write_text('{"job_1": {"id": ')  # half-written file
        with self.assertRaisesRegex(db_import.JsonImportError, "couldn't read"):
            self.run_import(["jobs_index.json"])
        self.assert_untouched("jobs_index.json")

    def test_crash_between_commit_and_rename(self):
        self.run_import(["locations.json"])
        # Simulate: committed, but the server died before renaming the file.
        (self.runs / "locations.json.bak").rename(self.runs / "locations.json")
        self.run_import(["locations.json"])
        self.assertEqual(db.count("locations"), 3)  # not imported twice
        self.assertFalse((self.runs / "locations.json").exists())
        self.assertTrue((self.runs / "locations.json.bak").exists())


class VerifyTests(ImportTestCase):
    def cli(self, *args):
        db.close()
        with redirect_stdout(io.StringIO()) as out:
            code = db_import.main([str(self.runs), *args])
        return code, out.getvalue()

    def test_verify_passes_after_clean_import(self):
        code, out = self.cli()
        self.assertEqual(code, 0, out)
        code, out = self.cli("--verify")
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFIED", out)

    def test_verify_names_a_changed_field(self):
        self.cli()
        v = next(iter(self.sample["videos_index.json"].values()))
        db.upsert("videos", {**v, "notes": "changed after import"})
        code, out = self.cli("--verify")
        self.assertEqual(code, 1)
        self.assertIn(f"videos {v['id']!r} field 'notes'", out)

    def test_verify_names_a_missing_record(self):
        self.cli()
        db.delete("locations", "Pond Overlook")
        code, out = self.cli("--verify")
        self.assertEqual(code, 1)
        self.assertIn("locations 'Pond Overlook' is missing", out)


if __name__ == "__main__":
    unittest.main()
