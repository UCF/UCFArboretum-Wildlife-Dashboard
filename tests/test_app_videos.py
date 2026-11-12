"""
End-to-end tests of app.py's Library (videos) storage on SQLite: starts the
real app against a sample runs/ folder, drives it through its HTTP API, and
checks the database matches what the app shows — including after a restart.

app.py keeps its data next to itself (BASE_DIR/runs), so each test copies
the app into a temporary folder first; your real runs/ is never touched.

Needs the app's own dependencies (flask, opencv, ...). Skipped without them.

    python -m unittest tests.test_app_videos -v
"""

import json
import unittest

from tests.app_harness import AppTestCase


class AppVideoStorageTests(AppTestCase):
    def test_first_start_imports_json(self):
        app_module, client = self.start_app()
        self.assertFalse((self.runs / "videos_index.json").exists())
        self.assertTrue((self.runs / "videos_index.json.bak").exists())
        self.assertEqual(app_module.videos, self.original)
        self.assertEqual(len(client.get("/api/videos").get_json()), 80)
        self.assert_db_matches(app_module)

    def test_edits_are_saved_and_survive_restart(self):
        app_module, client = self.start_app()
        ids = list(self.original)

        r = client.post(f"/api/videos/{ids[0]}/favorite", json={"favorited": True})
        self.assertEqual(r.status_code, 200)
        r = client.post(f"/api/videos/{ids[1]}/update",
                        json={"notes": "Two kits 🦝", "count": 2, "temperature": "71°F"})
        self.assertEqual(r.status_code, 200)
        r = client.post(f"/api/videos/{ids[2]}/correct", json={"species": "blank"})
        self.assertEqual(r.status_code, 200)
        r = client.post(f"/api/videos/{ids[3]}/delete")
        self.assertEqual(r.status_code, 200)
        self.assert_db_matches(app_module)

        restarted, _ = self.start_app()
        v = restarted.videos
        self.assertIs(v[ids[0]]["favorited"], True)
        self.assertEqual((v[ids[1]]["notes"], v[ids[1]]["count"]), ("Two kits 🦝", 2))
        self.assertIs(v[ids[1]]["metadata_edited"], True)
        self.assertEqual(v[ids[2]]["corrected_species"], "blank")
        self.assertEqual(v[ids[2]]["corrected_by"], "Test User")
        self.assertNotIn(ids[3], v)
        self.assertEqual(len(v), 79)

    def test_bulk_actions(self):
        app_module, client = self.start_app()
        marked = sum(1 for x in self.original.values() if x["marked_for_review"])
        r = client.post("/api/videos/clear-all-marks")
        self.assertEqual(r.get_json()["cleared_count"], marked)
        self.assertFalse(any(x["marked_for_review"] for x in app_module.videos.values()))
        self.assert_db_matches(app_module)

        # With every mark cleared, untouched blanks are now trash.
        expected_trash = sum(1 for x in app_module.videos.values() if app_module._is_trash(x))
        self.assertGreater(expected_trash, 0)
        r = client.post("/api/storage/empty-trash")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["result"]["deleted_count"], expected_trash)
        self.assert_db_matches(app_module)

    def test_finished_job_adds_and_resyncs_videos(self):
        app_module, _ = self.start_app()
        job_id = "job_new"
        out = self.runs / job_id / "predictions.json"
        out.parent.mkdir()
        out.write_text(json.dumps({
            "classification_categories": {"0": "blank", "1": "aves;new;bird"},
            "images": [
                {"file": "NEW_0001.JPG", "detections": [
                    {"conf": 0.9, "bbox": [0.3, 0.3, 0.2, 0.2], "classifications": [["1", 0.88]]}]},
                {"file": "NEW_0002.JPG", "detections": []},
            ],
        }))
        app_module.jobs[job_id] = {  # every field /api/run gives a job
            "id": job_id, "folder": str(self.root / "media"), "batch_id": None,
            "display_name": "New card", "ocr_config": "__skip_ocr__", "location": "Pond Overlook",
            "status": "done", "seq": 99, "started_at": None, "started_running_at": None,
            "finished_at": None, "output_json": str(out), "log_file": str(out.parent / "log.txt"),
            "cmd": [],
        }
        app_module.persist_jobs([job_id])
        app_module.sync_videos_from_job(job_id)
        new_id = app_module.video_id_for(job_id, "NEW_0001.JPG")
        self.assertEqual(app_module.videos[new_id]["ai_species"], "aves;new;bird")
        self.assertEqual(len(app_module.videos), 82)
        self.assert_db_matches(app_module)

        # Re-running the same job keeps human edits (and doesn't duplicate rows).
        app_module.videos[new_id].update(
            notes="keep me", corrected_species="aves;new;bird", corrected_by="Test User")
        app_module.persist_videos([new_id])
        app_module.sync_videos_from_job(job_id)
        self.assertEqual(app_module.videos[new_id]["notes"], "keep me")
        self.assertEqual(app_module.videos[new_id]["corrected_by"], "Test User")  # was being erased
        self.assertEqual(len(app_module.videos), 82)
        self.assert_db_matches(app_module)

    def test_location_merge_updates_videos(self):
        app_module, client = self.start_app()
        r = client.post("/api/locations/merge", json={
            "source_names": ["Pond Overlook", "Pine Flatwoods"], "target_name": "Pond Overlook",
            "lat": 28.5946, "lon": -81.1987,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(any(x["location"] == "Pine Flatwoods" for x in app_module.videos.values()))
        self.assert_db_matches(app_module)


if __name__ == "__main__":
    unittest.main()
