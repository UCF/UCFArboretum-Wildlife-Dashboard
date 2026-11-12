"""
End-to-end tests of app.py's jobs, upload batches and locations storage on
SQLite: drives the real app (in a temporary copy, see app_harness) through
an upload, a job run, and location edits, checking the database matches the
app after each step and across restarts.

    python -m unittest tests.test_app_jobs_uploads -v
"""

import io
import json
import sys
import unittest

import db
from tests.app_harness import AppTestCase


class AppJobsUploadsLocationsTests(AppTestCase):
    def test_first_start_imports_jobs_uploads_locations(self):
        app_module, _ = self.start_app()
        for name in ("jobs_index.json", "upload_batches_index.json", "locations.json"):
            self.assertFalse((self.runs / name).exists(), name)
            self.assertTrue((self.runs / f"{name}.bak").exists(), name)

        original_batches = json.loads((self.runs / "upload_batches_index.json.bak").read_text())
        self.assertEqual(app_module.upload_batches, original_batches)
        self.assertEqual(len(app_module.jobs), 4)
        self.assertEqual(set(app_module.locations), {"Pond Overlook", "Oak Hammock Trail", "Pine Flatwoods"})
        self.assert_db_matches(app_module)

    def test_upload_then_job_run(self):
        app_module, client = self.start_app()

        # 1. Declare a two-file upload batch.
        r = client.post("/api/uploads", json={"files": [
            {"path": "CLIP_A.MP4", "size": 5}, {"path": "CLIP_B.MP4", "size": 3},
        ]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        batch_id = r.get_json()["batch_id"]
        self.assertEqual(app_module.upload_batches[batch_id]["status"], "uploading")
        self.assert_db_matches(app_module)

        # 2. Send one file's only chunk, then "restart" mid-upload: progress must survive.
        r = client.post(f"/api/uploads/{batch_id}/chunk", data={
            "path": "CLIP_A.MP4", "chunk_index": "0", "chunk": (io.BytesIO(b"aaaaa"), "chunk"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_db_matches(app_module)

        app_module, client = self.start_app()
        files = app_module.upload_batches[batch_id]["files"]
        self.assertEqual(files["CLIP_A.MP4"]["received_chunks"], [0])
        self.assertEqual(files["CLIP_B.MP4"]["received_chunks"], [])

        r = client.post(f"/api/uploads/{batch_id}/chunk", data={
            "path": "CLIP_B.MP4", "chunk_index": "0", "chunk": (io.BytesIO(b"bbb"), "chunk"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        # 3. Submit it as a job, with a stand-in for SpeciesNet that writes no
        #    output — so the job ends "done" or "error" depending on the
        #    machine; what matters is every status change was saved.
        with self.fake_speciesnet():
            r = client.post("/api/run", json={
                "batch_id": batch_id, "folder_name": "Test card", "ocr_config": "__skip_ocr__",
                "location": "Pond Overlook",
            })
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(app_module.upload_batches[batch_id]["status"], "processing")
            self.wait_for_jobs_idle(app_module)
        new_jobs = [j for j in app_module.jobs.values() if j.get("batch_id") == batch_id]
        self.assertEqual(len(new_jobs), 1)
        self.assertIn(new_jobs[0]["status"], ("done", "error"))
        self.assertIsNotNone(new_jobs[0]["finished_at"])
        self.assert_db_matches(app_module)

        restarted, _ = self.start_app()
        self.assertEqual(restarted.jobs[new_jobs[0]["id"]], new_jobs[0])

    def test_cancel_upload_deletes_batch_and_its_files(self):
        app_module, client = self.start_app()
        r = client.post("/api/uploads", json={"files": [{"path": "X.MP4", "size": 4}]})
        batch_id = r.get_json()["batch_id"]
        r = client.delete(f"/api/uploads/{batch_id}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn(batch_id, app_module.upload_batches)
        self.assert_db_matches(app_module)
        n = db.connect().execute("SELECT COUNT(*) FROM upload_batch_files WHERE batch_id = ?", (batch_id,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_interrupted_job_is_requeued_and_saved(self):
        # Simulate a server that died mid-job: one job left "running" on disk.
        jobs = json.loads((self.runs / "jobs_index.json").read_text())
        stuck_id = next(iter(jobs))
        jobs[stuck_id]["status"] = "running"
        # Real folders for its log/output, as a real job would have.
        job_dir = self.runs / stuck_id
        job_dir.mkdir()
        jobs[stuck_id]["log_file"] = str(job_dir / "log.txt")
        jobs[stuck_id]["output_json"] = str(job_dir / "predictions.json")
        (self.runs / "jobs_index.json").write_text(json.dumps(jobs))

        with self.fake_speciesnet():
            app_module, _ = self.start_app()
            self.wait_for_jobs_idle(app_module)  # the worker picks it back up and finishes it
        self.assertNotIn(app_module.jobs[stuck_id]["status"], ("running", "queued"))
        self.assert_db_matches(app_module)

    def test_location_edits(self):
        app_module, client = self.start_app()

        r = client.post("/api/locations/import-csv/commit", json={"rows": [
            {"name": "Lake Claire", "lat": 28.6011, "lon": -81.2003},
            {"name": "Bad Row", "lat": 999, "lon": 0},
        ]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("Lake Claire", app_module.locations)
        self.assertNotIn("Bad Row", app_module.locations)
        self.assert_db_matches(app_module)

        r = client.post("/api/locations/delete", json={"name": "Oak Hammock Trail"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_db_matches(app_module)

        restarted, _ = self.start_app()
        self.assertEqual(set(restarted.locations), {"Pond Overlook", "Pine Flatwoods", "Lake Claire"})
        self.assertEqual(restarted.locations["Lake Claire"], {"lat": 28.6011, "lon": -81.2003})


    # --- the job worker must survive any one job failing -----------------

    def enqueue_job(self, app_module, job_id, *, log_dir_exists=True, predictions=None):
        """Queues a job whose command succeeds instantly (no SpeciesNet needed)."""
        job_dir = self.runs / job_id
        if log_dir_exists:
            job_dir.mkdir()
        output_json = job_dir / "predictions.json"
        if predictions is not None:
            output_json.write_text(predictions)
        with app_module.jobs_lock:
            app_module.jobs[job_id] = {
                "id": job_id, "folder": str(self.root / "media"), "batch_id": None,
                "display_name": job_id, "ocr_config": "__skip_ocr__", "location": "Pond Overlook",
                "status": "queued", "seq": 1000 + len(app_module.jobs), "started_at": None,
                "started_running_at": None, "finished_at": None, "output_json": str(output_json),
                "log_file": str(job_dir / "log.txt"), "cmd": [sys.executable, "-c", "pass"],
            }
        app_module.persist_jobs([job_id])
        with app_module.queue_cv:
            app_module.job_queue.append(job_id)
            app_module.queue_cv.notify()

    def test_worker_survives_missing_log_folder(self):
        app_module, _ = self.start_app()
        self.enqueue_job(app_module, "job_no_log_dir", log_dir_exists=False)
        self.enqueue_job(app_module, "job_after", predictions='{"images": []}')
        self.wait_for_jobs_idle(app_module, timeout=20)
        self.assertEqual(app_module.jobs["job_no_log_dir"]["status"], "error")
        self.assertEqual(app_module.jobs["job_after"]["status"], "done")  # the worker kept going
        self.assertTrue(app_module.worker_thread.is_alive())
        self.assert_db_matches(app_module)

    def test_worker_survives_failure_after_job_finishes(self):
        # The command succeeds, but its output is corrupt, so adding the
        # results to the Library fails. That used to kill the worker too.
        app_module, _ = self.start_app()
        self.enqueue_job(app_module, "job_bad_output", predictions='{"images": [')
        self.enqueue_job(app_module, "job_after", predictions='{"images": []}')
        self.wait_for_jobs_idle(app_module, timeout=20)
        self.assertEqual(app_module.jobs["job_bad_output"]["status"], "error")
        log = (self.runs / "job_bad_output" / "log.txt").read_text()
        self.assertIn("Unexpected error while processing this job", log)
        self.assertEqual(app_module.jobs["job_after"]["status"], "done")
        self.assertTrue(app_module.worker_thread.is_alive())
        self.assert_db_matches(app_module)


if __name__ == "__main__":
    unittest.main()
