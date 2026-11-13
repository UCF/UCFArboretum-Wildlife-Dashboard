"""
Realistic fake data in the exact shapes app.py keeps in its JSON files
under runs/. Used by the tests, and runnable to write a sample runs/ folder:

    python tests/sample_data.py /path/to/sample_runs [--videos 500]

Deterministic (fixed random seed), so test failures are reproducible.
"""

import hashlib
import json
import random
import sys
from pathlib import Path

LOCATIONS = {
    "Pond Overlook": {"lat": 28.5946, "lon": -81.1987},
    "Oak Hammock Trail": {"lat": 28.5951, "lon": -81.1972},
    "Pine Flatwoods": {"lat": 28.5932, "lon": -81.1995},
}

SPECIES = [
    "blank",
    "mammalia;cingulata;dasypodidae;dasypus;novemcinctus;nine-banded armadillo",
    "mammalia;didelphimorphia;didelphidae;didelphis;virginiana;virginia opossum",
    "mammalia;carnivora;procyonidae;procyon;lotor;common raccoon",
    "mammalia;artiodactyla;cervidae;odocoileus;virginianus;white-tailed deer",
    "mammalia;rodentia;sciuridae;sciurus;carolinensis;eastern gray squirrel",
    "aves;gruiformes;gruidae;antigone;canadensis;sandhill crane",
    "reptilia;testudines;testudinidae;gopherus;polyphemus;gopher tortoise",
]

USER_NAMES = ["Alex Rivera", "Sam Chen", "Jordan Patel"]


def _video_id(job_id, filename):
    return hashlib.sha1(f"{job_id}:{filename}".encode()).hexdigest()[:16]


def make_sample(n_videos=60, seed=1):
    """Returns {json filename: contents}, matching what app.py writes to runs/."""
    rng = random.Random(seed)
    jobs, videos, batches = {}, {}, {}
    n_jobs = max(1, n_videos // 20)
    per_job = -(-n_videos // n_jobs)

    for j in range(n_jobs):
        job_id = f"job_20260{(j % 9) + 1}01_{j:04d}"
        batch_id = f"batch_{j:04d}"
        folder = f"/srv/arb/runs/uploads/{batch_id}"
        location = rng.choice(list(LOCATIONS))
        status = "done" if j < n_jobs - 1 else rng.choice(["done", "queued", "failed"])
        jobs[job_id] = {
            "id": job_id,
            "folder": folder,
            "batch_id": batch_id,
            "display_name": f"SD card {j + 1}",
            "ocr_config": "Browning 1080p" if j % 3 else "__skip_ocr__",
            "location": location,
            "status": status,
            "seq": j + 1,
            "started_at": f"2026-0{(j % 9) + 1}-01T09:00:00",
            "started_running_at": f"2026-0{(j % 9) + 1}-01T09:00:05" if status != "queued" else None,
            "finished_at": f"2026-0{(j % 9) + 1}-01T09:42:10" if status != "queued" else None,
            "output_json": f"/srv/arb/runs/{job_id}/predictions.json",
            "log_file": f"/srv/arb/runs/{job_id}/log.txt",
            "cmd": ["python", "-m", "speciesnet.scripts.run_model", "--folders", folder,
                    "--predictions_json", f"/srv/arb/runs/{job_id}/predictions.json"],
        }
        batch_files = {}
        for k in range(per_job):
            if len(videos) >= n_videos:
                break
            is_photo = rng.random() < 0.15
            filename = f"IMG_{k:04d}.JPG" if is_photo else f"VID_{k:04d}.MP4"
            size = rng.randint(2_000_000, 80_000_000)
            chunks = -(-size // (8 * 1024 * 1024))
            batch_files[filename] = {
                "size": size, "expected_chunks": chunks, "received_chunks": list(range(chunks)),
            }

            vid = _video_id(job_id, filename)
            ai_species = rng.choice(SPECIES[1:]) if rng.random() < 0.4 else None
            corrected = None
            if rng.random() < 0.25:
                corrected = rng.choice(SPECIES)
            edited = rng.random() < 0.1
            videos[vid] = {
                "id": vid,
                "job_id": job_id,
                "folder": folder,
                "filename": filename,
                "media_type": "photo" if is_photo else "video",
                "ai_species": ai_species,
                "ai_classifier_conf": round(rng.uniform(0.3, 0.99), 2) if ai_species else None,
                "ai_detector_conf": round(rng.uniform(0.2, 0.99), 2) if ai_species else None,
                "corrected_species": corrected,
                "favorited": rng.random() < 0.05,
                "marked_for_review": corrected is None,
                "corrected_at": "2026-10-03T14:22:51" if corrected else None,
                "corrected_by": rng.choice(USER_NAMES + [None]) if corrected else None,
                "date": f"2026-0{(j % 9) + 1}-{rng.randint(1, 28):02d}" if rng.random() > 0.05 else None,
                "time": f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}",
                "location": location,
                "temperature": f"{rng.randint(45, 95)}°F" if rng.random() > 0.1 else None,
                "diel_period": rng.choice(["Day", "Night", "Dawn", "Dusk"]),
                "count": rng.randint(1, 3) if (ai_species or corrected) else 0,
                "notes": rng.choice(["", "", "", "Two juveniles following", "Possible injury on left leg — check again",
                                     'Quote "test" & ünïcode 🦝']),
                "display_filename": filename,
                "metadata_edited": edited,
                "has_bar_crop": not is_photo,
                "has_thumbnail": not is_photo,
            }
        batches[batch_id] = {
            "status": "processing",
            "created_at": f"2026-0{(j % 9) + 1}-01T08:30:00",
            "updated_at": f"2026-0{(j % 9) + 1}-01T08:59:00",
            "files": batch_files,
        }

    # One upload still in flight: partially received chunks, no job yet.
    batches["batch_inflight"] = {
        "status": "uploading",
        "created_at": "2026-11-12T10:00:00",
        "updated_at": "2026-11-12T10:05:00",
        "files": {"VID_0001.MP4": {"size": 30_000_000, "expected_chunks": 4, "received_chunks": [0, 2]}},
    }

    return {
        "jobs_index.json": jobs,
        "videos_index.json": videos,
        "upload_batches_index.json": batches,
        "locations.json": LOCATIONS,
        "species_list.json": sorted(SPECIES),
        "category_settings.json": {
            SPECIES[3]: {"private": False, "cover": "cover_raccoon.jpg", "cover_version": 2},
            SPECIES[7]: {"private": True},
        },
        "storage_settings.json": {
            "auto_empty_enabled": True,
            "auto_empty_threshold": 90,
            "last_auto_empty": {"at": "2026-11-01T03:00:00", "freed_bytes": 12_345_678_901, "deleted": 412},
        },
        "ocr_configs.json": {
            "configs": {"Browning 1080p": {"bar_box": [0, 1012, 1920, 1080], "ref_width": 1920, "ref_height": 1080}},
            "last_used": "Browning 1080p",
            "disabled": False,
        },
        "users.json": {
            "arivera": {"password_hash": "scrypt:32768:8:1$fake$abc123", "first_name": "Alex", "last_name": "Rivera"},
            "schen": {"password_hash": "scrypt:32768:8:1$fake$def456", "first_name": "Sam", "last_name": "Chen"},
        },
        "migrations.json": ["blank_count_zero"],
    }


def write_sample(target_dir, n_videos=60):
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    for name, contents in make_sample(n_videos).items():
        (target / name).write_text(json.dumps(contents, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    n = int(sys.argv[sys.argv.index("--videos") + 1]) if "--videos" in sys.argv else 60
    print(f"Wrote sample data to {write_sample(sys.argv[1], n)}")
