"""
Manage accounts for the app's username+password sign-in.

There is deliberately no sign-up flow in the app itself — accounts are
added here, out of band, by whoever runs the server. Run this on the same
machine as app.py, since it writes directly to the users table in
runs/wildlife.db — the table app.py's /api/auth/login route reads on every
login attempt (see get_user() in app.py). You can run this while the
server is running; a newly-added account works immediately, no restart
needed.

If runs/users.json from before the SQLite switch is still there (the
server hasn't been started on the new version yet), its accounts are
imported first, exactly as the server would.

Usage:
    python manage_users.py add <username> <first_name> <last_name>
        Prompts for a password (twice, to catch typos) rather than taking
        it as an argument, so it never ends up in shell history.
    python manage_users.py list
    python manage_users.py remove <username>
"""

import getpass
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

import db
import db_import

RUNS_DIR = Path(__file__).parent / "runs"


def open_db():
    """Opens runs/wildlife.db, importing a leftover users.json first if there is one."""
    db.DB_PATH = RUNS_DIR / "wildlife.db"
    db_import.import_sources(RUNS_DIR, ["users.json"], log=lambda msg: None)


def cmd_add(args):
    if len(args) != 3:
        print("Usage: python manage_users.py add <username> <first_name> <last_name>")
        sys.exit(1)
    username, first_name, last_name = args
    username = username.strip().lower()

    password = getpass.getpass("Password: ")
    if not password:
        print("Password can't be empty.")
        sys.exit(1)
    if getpass.getpass("Confirm password: ") != password:
        print("Passwords didn't match.")
        sys.exit(1)

    open_db()
    is_update = db.get("users", username) is not None
    db.upsert("users", {
        "username": username,
        "password_hash": generate_password_hash(password),
        "first_name": first_name,
        "last_name": last_name,
    })
    verb = "Updated" if is_update else "Added"
    print(f"{verb} user '{username}' ({first_name} {last_name}).")


def cmd_list(_args):
    open_db()
    users = db.load_all("users")
    if not users:
        print("No users yet.")
        return
    for username, u in sorted(users.items()):
        print(f"{username}: {u['first_name']} {u['last_name']}")


def cmd_remove(args):
    if len(args) != 1:
        print("Usage: python manage_users.py remove <username>")
        sys.exit(1)
    username = args[0].strip().lower()
    open_db()
    if not db.delete("users", username):
        print(f"No such user: {username}")
        sys.exit(1)
    print(f"Removed user '{username}'.")


COMMANDS = {"add": cmd_add, "list": cmd_list, "remove": cmd_remove}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
