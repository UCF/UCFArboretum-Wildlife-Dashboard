"""
One-time import of the old runs/*.json files into the SQLite database.

Each JSON file is imported on its own, and only once:

  1. Read the JSON file and convert its records to table rows (filling in
     defaults for fields older versions of the app didn't write yet).
  2. Inside ONE transaction: insert every row, check the row count matches
     the file, and record the import in the `migrations` table under
     "json_import:<file name>". Any error rolls the whole file back.
  3. Only after that commit succeeds, rename the file to <name>.bak. The
     original data is never deleted, so a bad import can always be redone.

Re-running is safe: an already-imported file is skipped (and if the server
died between steps 2 and 3, the leftover JSON file is just renamed).

app.py imports each file at startup in the same step it switches that data
over to SQLite (see the sources it passes to import_sources). To try it by
hand on a COPY of a runs/ folder:

    python db_import.py /path/to/runs_copy --dry-run   # report only, changes nothing
    python db_import.py /path/to/runs_copy             # import everything
    python db_import.py /path/to/runs_copy --verify    # then check every record came across exactly
"""

import argparse
import json
import sys
from pathlib import Path

import db


class JsonImportError(Exception):
    """A JSON file couldn't be imported; nothing from it was saved."""


# --------------------------------------------------------------------------
# Per-file converters: JSON contents -> {table: [row dicts]} plus settings.
# Each returns (rows_by_table, settings, expected_counts).
# --------------------------------------------------------------------------

# Same defaults app.py fills in for video records written before these
# fields existed (see _NEW_FIELD_DEFAULTS there).
_VIDEO_DEFAULTS = {
    "date": None, "time": None, "location": None, "diel_period": None,
    "temperature": None, "count": 0, "notes": "", "display_filename": None,
    "metadata_edited": False, "has_bar_crop": False, "media_type": "video",
    "has_thumbnail": False, "corrected_by": None, "corrected_species": None,
    "corrected_at": None, "favorited": False, "ai_species": None,
    "ai_classifier_conf": None, "ai_detector_conf": None,
}

# Columns declared NOT NULL: an explicit null in old JSON becomes this.
_NOT_NULL_FALLBACKS = {
    "count": 0, "notes": "", "media_type": "video", "favorited": False,
    "metadata_edited": False, "has_bar_crop": False, "has_thumbnail": False,
}


def _convert_videos(data):
    rows = []
    for vid, v in data.items():
        row = dict(v)
        row.setdefault("id", vid)
        for key, default in _VIDEO_DEFAULTS.items():
            row.setdefault(key, row.get("filename") if key == "display_filename" else default)
        # Same rule as app.py: entries predating the review flag start marked
        # only if their species hasn't been confirmed yet.
        row.setdefault("marked_for_review", not bool(row.get("corrected_species")))
        for key, fallback in _NOT_NULL_FALLBACKS.items():
            if row.get(key) is None:
                row[key] = fallback
        if row.get("marked_for_review") is None:
            row["marked_for_review"] = not bool(row.get("corrected_species"))
        rows.append(row)
    return {"videos": rows}, {}, {"videos": len(data)}


def _convert_jobs(data):
    rows = []
    for job_id, j in data.items():
        row = dict(j)
        row.setdefault("id", job_id)
        rows.append(row)
    return {"jobs": rows}, {}, {"jobs": len(data)}


def _convert_upload_batches(data):
    batches, files = [], []
    for batch_id, b in data.items():
        b = dict(b)
        for rel, f in (b.pop("files", None) or {}).items():
            files.append({"batch_id": batch_id, "rel_path": rel, **f})
        batches.append({"id": batch_id, **b})
    return (
        {"upload_batches": batches, "upload_batch_files": files}, {},
        {"upload_batches": len(batches), "upload_batch_files": len(files)},
    )


def _convert_locations(data):
    rows = [{"name": name, **coords} for name, coords in data.items()]
    return {"locations": rows}, {}, {"locations": len(rows)}


def _convert_species(data):
    labels = sorted(set(data))  # the file is a list; guard against duplicates
    return {"species": [{"label": s} for s in labels]}, {}, {"species": len(labels)}


def _convert_categories(data):
    rows = []
    for label, cfg in data.items():
        row = {"label": label, **cfg}
        row["private"] = bool(row.get("private", False))
        row["cover_version"] = row.get("cover_version") or 0
        rows.append(row)
    return {"categories": rows}, {}, {"categories": len(rows)}


def _convert_ocr_configs(data):
    configs = data.get("configs", {}) or {}
    rows = [{"name": name, **cfg} for name, cfg in configs.items()]
    settings = {"ocr_last_used": data.get("last_used"), "ocr_disabled": bool(data.get("disabled", False))}
    return {"ocr_configs": rows}, settings, {"ocr_configs": len(rows)}


def _convert_storage_settings(data):
    return {}, {"storage": data}, {}


def _convert_users(data):
    rows = [{"username": u, **rec} for u, rec in data.items()]
    for row in rows:
        row.setdefault("first_name", "")
        row.setdefault("last_name", "")
    return {"users": rows}, {}, {"users": len(rows)}


def _convert_migrations(data):
    # Stored straight into the migrations table (not via upsert), see import_file.
    return {}, {}, {"migrations": len(set(data))}


# file name -> (converter, empty value if the file doesn't exist)
SOURCES = {
    "videos_index.json": _convert_videos,
    "jobs_index.json": _convert_jobs,
    "upload_batches_index.json": _convert_upload_batches,
    "locations.json": _convert_locations,
    "species_list.json": _convert_species,
    "category_settings.json": _convert_categories,
    "ocr_configs.json": _convert_ocr_configs,
    "storage_settings.json": _convert_storage_settings,
    "users.json": _convert_users,
    "migrations.json": _convert_migrations,
}


def _marker(filename):
    return f"json_import:{filename}"


def _backup_path(path):
    """runs/x.json -> runs/x.json.bak (or .bak.1, .bak.2 ... if that's taken)."""
    candidate = path.with_name(path.name + ".bak")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{n}")
        n += 1
    return candidate


def import_file(runs_dir, filename, dry_run=False):
    """
    Imports one JSON file. Returns a short human-readable status line.
    Raises JsonImportError (with nothing saved) if the file can't be imported.
    """
    if filename not in SOURCES:
        raise ValueError(f"Unknown source file {filename!r}")
    path = Path(runs_dir) / filename
    already = _marker(filename) in db.applied_migrations()

    if already:
        if path.exists() and not dry_run:
            # Imported, but the server stopped before the rename. Finish it.
            path.rename(_backup_path(path))
            return f"{filename}: already imported; moved leftover file to .bak"
        return f"{filename}: already imported"

    if not path.exists():
        if not dry_run:
            db.mark_migration_applied(_marker(filename))  # nothing to import, ever
        return f"{filename}: no file (fresh install), nothing to import"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JsonImportError(f"{filename}: couldn't read it as JSON ({e}). The file is unchanged.") from e

    try:
        rows_by_table, settings, expected = SOURCES[filename](data)
    except (AttributeError, TypeError) as e:
        raise JsonImportError(f"{filename}: unexpected shape ({e}). The file is unchanged.") from e

    # Catch fields with no column BEFORE writing anything, and name them all.
    for table, rows in rows_by_table.items():
        unknown = set().union(*(set(r) for r in rows)) - set(db.COLUMNS[table]["fields"]) if rows else set()
        if unknown:
            raise JsonImportError(
                f"{filename}: field(s) {sorted(unknown)} have no column in table {table!r}. "
                "Add them to db.SCHEMA/db.COLUMNS so no data is lost. The file is unchanged."
            )

    summary = ", ".join(f"{n} {t}" for t, n in expected.items()) or "settings"
    if dry_run:
        return f"{filename}: would import {summary}"

    try:
        with db.transaction() as conn:
            # Re-check now that we hold the write lock: another process (the
            # server and manage_users.py can both import users.json) may have
            # finished this same import while we were reading the file.
            if conn.execute("SELECT 1 FROM migrations WHERE name = ?", (_marker(filename),)).fetchone():
                return f"{filename}: already imported (by another process)"
            for table, rows in rows_by_table.items():
                for row in rows:
                    db.upsert(table, row, conn=conn)
            for key, value in settings.items():
                db.set_setting(key, value, conn=conn)
            if filename == "migrations.json":
                for name in data:
                    db.mark_migration_applied(name, conn=conn)
            for table, n in expected.items():
                got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if got < n:
                    raise JsonImportError(f"{filename}: expected {n} {table} rows, database has {got}")
            db.mark_migration_applied(_marker(filename), conn=conn)
    except JsonImportError:
        raise
    except Exception as e:
        raise JsonImportError(f"{filename}: import failed and was rolled back ({e}). The file is unchanged.") from e

    try:
        path.rename(_backup_path(path))
    except FileNotFoundError:
        pass  # another process already renamed it
    return f"{filename}: imported {summary}; original kept as {path.name}.bak"


def import_sources(runs_dir, filenames, dry_run=False, log=print):
    """Imports several files in order; each is its own all-or-nothing unit."""
    db.init_db()
    for filename in filenames:
        log(import_file(runs_dir, filename, dry_run=dry_run))


def verify_file(runs_dir, filename):
    """
    Compares an imported file's original (<name>.json.bak) against the
    database, record by record and field by field, using the same
    conversion as the import. Returns a list of problems (empty = exact).

    Meant to be run right after an import, before anyone edits anything —
    once the app is in use, the database legitimately moves on from the
    original file.
    """
    bak = Path(runs_dir) / f"{filename}.bak"
    if not bak.exists():
        return []  # nothing was imported from this file (e.g. fresh install)
    data = json.loads(bak.read_text(encoding="utf-8"))
    rows_by_table, settings, _ = SOURCES[filename](data)
    problems = []
    for table, rows in rows_by_table.items():
        stored = db.load_all(table)
        for row in rows:
            key = db._key_value(table, row)
            got = stored.get(key)
            if got is None:
                problems.append(f"{filename}: {table} {key!r} is missing from the database")
                continue
            for field, value in row.items():
                if got.get(field) != value:
                    problems.append(
                        f"{filename}: {table} {key!r} field {field!r}: "
                        f"file has {value!r}, database has {got.get(field)!r}"
                    )
        extra = set(stored) - {db._key_value(table, r) for r in rows}
        if extra:
            problems.append(f"{filename}: {table} has {len(extra)} row(s) not in the file, e.g. {sorted(extra)[:3]}")
    for key, value in settings.items():
        if db.get_setting(key) != value:
            problems.append(f"{filename}: setting {key!r}: file has {value!r}, database has {db.get_setting(key)!r}")
    if filename == "migrations.json":
        missing = set(data) - db.applied_migrations()
        if missing:
            problems.append(f"{filename}: migrations not recorded: {sorted(missing)}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs_dir", type=Path, help="folder holding the *.json files (the database is created here too)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report what would be imported; change nothing")
    mode.add_argument("--verify", action="store_true",
                      help="after an import: compare every record in the .bak files with the database")
    args = parser.parse_args(argv)

    if args.verify:
        db.DB_PATH = args.runs_dir / "wildlife.db"
        if not db.DB_PATH.exists():
            print(f"ERROR: no database at {db.DB_PATH}; import first", file=sys.stderr)
            return 1
        problems = []
        for filename in SOURCES:
            found = verify_file(args.runs_dir, filename)
            print(f"{filename}: {'OK' if not found else f'{len(found)} problem(s)'}")
            problems += found
        for line in problems[:50]:
            print(f"  {line}")
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
        print("VERIFIED: every record matches." if not problems else f"MISMATCHES: {len(problems)}")
        return 0 if not problems else 1

    db.DB_PATH = args.runs_dir / "wildlife.db"
    if args.dry_run and not db.DB_PATH.exists():
        # Don't leave an empty database file behind just from looking.
        db.DB_PATH = Path(":memory:")
    try:
        import_sources(args.runs_dir, list(SOURCES), dry_run=args.dry_run)
    except JsonImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
