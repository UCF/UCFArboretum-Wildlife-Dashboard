"""
Tests for db.py. Run from the repo root with:

    python -m unittest discover tests -v

Each test uses its own throwaway database file, never runs/wildlife.db.
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import db
from tests.sample_data import make_sample


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        db.init_db()
        self.sample = make_sample(n_videos=60)

    def tearDown(self):
        db.close()
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()


class SchemaTests(DbTestCase):
    def test_all_tables_created(self):
        names = {r[0] for r in db.connect().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(set(db.COLUMNS) | {"settings", "migrations"} <= names)

    def test_columns_dict_matches_real_tables(self):
        # COLUMNS (Python) and SCHEMA (SQL) describe the same tables; catch drift.
        for table, spec in db.COLUMNS.items():
            real = [r["name"] for r in db.connect().execute(f"PRAGMA table_info({table})")]
            self.assertEqual(sorted(real), sorted(spec["fields"]), table)

    def test_init_is_repeatable_and_versioned(self):
        db.init_db()
        db.init_db()
        self.assertEqual(db.connect().execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(db.connect().execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_refuses_newer_schema(self):
        db.connect().execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            db.init_db()

    def test_every_sample_video_field_has_a_column(self):
        # If app.py's record shape and the schema ever diverge, this fails first.
        for v in self.sample["videos_index.json"].values():
            self.assertEqual(set(v), set(db.COLUMNS["videos"]["fields"]))
        for j in self.sample["jobs_index.json"].values():
            self.assertEqual(set(j), set(db.COLUMNS["jobs"]["fields"]))


class RoundTripTests(DbTestCase):
    """What goes in must come back out identical — types included."""

    def test_videos(self):
        videos = self.sample["videos_index.json"]
        db.upsert_many("videos", videos.values())
        self.assertEqual(db.load_all("videos"), videos)
        one = next(iter(videos.values()))
        self.assertIs(db.get("videos", one["id"])["favorited"], one["favorited"])  # real bools, not 0/1

    def test_jobs_with_list_column(self):
        jobs = self.sample["jobs_index.json"]
        db.upsert_many("jobs", jobs.values())
        self.assertEqual(db.load_all("jobs"), jobs)

    def test_locations_species_users(self):
        locs = self.sample["locations.json"]
        db.upsert_many("locations", ({"name": n, **v} for n, v in locs.items()))
        self.assertEqual({n: {"lat": r["lat"], "lon": r["lon"]} for n, r in db.load_all("locations").items()}, locs)

        db.upsert_many("species", ({"label": s} for s in self.sample["species_list.json"]))
        self.assertEqual(sorted(db.load_all("species")), self.sample["species_list.json"])

        users = self.sample["users.json"]
        db.upsert_many("users", ({"username": u, **v} for u, v in users.items()))
        self.assertEqual(db.get("users", "schen")["first_name"], "Sam")

    def test_upload_batch_files_and_cascade(self):
        batch = self.sample["upload_batches_index.json"]["batch_inflight"]
        db.upsert("upload_batches", {"id": "batch_inflight", **{k: v for k, v in batch.items() if k != "files"}})
        for rel, f in batch["files"].items():
            db.upsert("upload_batch_files", {"batch_id": "batch_inflight", "rel_path": rel, **f})
        got = db.get("upload_batch_files", ("batch_inflight", "VID_0001.MP4"))
        self.assertEqual(got["received_chunks"], [0, 2])

        db.delete("upload_batches", "batch_inflight")
        self.assertEqual(db.count("upload_batch_files"), 0)  # removed with its batch

    def test_settings_and_migrations(self):
        storage = self.sample["storage_settings.json"]
        db.set_setting("storage", storage)
        self.assertEqual(db.get_setting("storage"), storage)
        self.assertEqual(db.get_setting("missing", "dflt"), "dflt")

        db.mark_migration_applied("blank_count_zero")
        db.mark_migration_applied("blank_count_zero")  # second time is a no-op
        self.assertEqual(db.applied_migrations(), {"blank_count_zero"})


class WriteBehaviourTests(DbTestCase):
    def test_upsert_updates_in_place(self):
        v = next(iter(self.sample["videos_index.json"].values()))
        db.upsert("videos", v)
        db.upsert("videos", {**v, "notes": "edited", "favorited": True})
        self.assertEqual(db.count("videos"), 1)
        self.assertEqual(db.get("videos", v["id"])["notes"], "edited")

    def test_unknown_field_is_rejected_not_dropped(self):
        v = next(iter(self.sample["videos_index.json"].values()))
        with self.assertRaises(ValueError):
            db.upsert("videos", {**v, "brand_new_field": 1})

    def test_failed_transaction_saves_nothing(self):
        videos = list(self.sample["videos_index.json"].values())
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                db.upsert("videos", videos[0], conn=conn)
                db.upsert("videos", {**videos[1], "job_id": None}, conn=conn)  # violates NOT NULL
        self.assertEqual(db.count("videos"), 0)  # the first write was rolled back too

    def test_delete(self):
        v = next(iter(self.sample["videos_index.json"].values()))
        db.upsert("videos", v)
        self.assertTrue(db.delete("videos", v["id"]))
        self.assertFalse(db.delete("videos", v["id"]))
        self.assertIsNone(db.get("videos", v["id"]))

    def test_concurrent_writers(self):
        # The server writes from many threads at once; none may be lost.
        videos = list(make_sample(n_videos=400, seed=2)["videos_index.json"].values())
        errors = []

        def worker(chunk):
            try:
                for v in chunk:
                    db.upsert("videos", v)
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(videos[i::8],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(db.count("videos"), len(videos))


if __name__ == "__main__":
    unittest.main()
