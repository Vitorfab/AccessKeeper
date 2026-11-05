"""Verify the AccessKeeper audit log hash chain without starting the app.

Standard library only. The database is opened read-only, so running this never
changes anything. Exit code: 0 = intact, 1 = tampering found, 2 = could not verify.

Usage:  python verify_audit.py [--db PATH] [--top HASH]
The key comes from AUDIT_HASH_KEY or SECRET_KEY (environment or the .env file).
--top compares the top of the chain with a hash you copied earlier.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ZERO_HASH = "0" * 64
CHAIN_START_ACTION = "audit.chain_started"
KEY_INFO = b"accesskeeper audit log hash chain v1"
START_DETAILS = re.compile(
    r"legacy_events=(\d+) \| last_legacy_id=(\d+) \| legacy_digest=([0-9a-f]{64}) \| key_fp=([0-9a-f]{16})"
)
BATCH = 1000
OK, BROKEN, CANNOT = 0, 1, 2


def derive_key(secret):
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, KEY_INFO + b"\x01", hashlib.sha256).digest()


def key_fingerprint(key):
    return hmac.new(key, b"accesskeeper audit key fingerprint", hashlib.sha256).hexdigest()[:16]


def stamp(value):
    if isinstance(value, str):
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in value else "%Y-%m-%d %H:%M:%S"
        value = datetime.strptime(value, fmt)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def canonical(values):
    return json.dumps(values, separators=(",", ":")).encode("utf-8")


def entry_hash(key, prev_hash, row):
    _id, ts, actor, action, target, details, ip = row[:7]
    return hmac.new(key, canonical([prev_hash, stamp(ts), actor, action, target, details, ip]), hashlib.sha256).hexdigest()


COLUMNS = "id, timestamp, actor, action, target, details, ip, prev_hash, hash"


def rows(conn, where, params=()):
    last = 0
    while True:
        batch = conn.execute(
            f"SELECT {COLUMNS} FROM audit_log WHERE id > ? AND {where} ORDER BY id LIMIT {BATCH}",
            (last, *params),
        ).fetchall()
        if not batch:
            return
        yield from batch
        last = batch[-1][0]


def legacy_digest(conn, before_id):
    digest = hashlib.sha256()
    count = 0
    for r in rows(conn, "hash IS NULL AND id < ?", (before_id,)):
        digest.update(canonical([r[0], stamp(r[1]), r[2], r[3], r[4], r[5], r[6]]) + b"\n")
        count += 1
    return count, digest.hexdigest()


def broken(row_id, reason, checked):
    where = f" at event #{row_id}" if row_id else ""
    return BROKEN, f"FAILED{where}: {reason}. Events from there on cannot be trusted.", checked, None


def verify(conn, key, expected_top=None):
    """Return (code, message, chained_events_checked, top_hash)."""
    try:
        head = conn.execute("SELECT last_hash FROM audit_chain WHERE id = 1").fetchone()
    except sqlite3.DatabaseError as exc:
        return CANNOT, f"This does not look like an AccessKeeper database ({exc}).", 0, None
    if head is None:
        return CANNOT, "The hash chain has not been started yet. Start the app once to begin it.", 0, None

    previous = start = top = None
    checked = 0
    for row in rows(conn, "hash IS NOT NULL"):
        if previous is None:
            match = START_DETAILS.fullmatch(row[5] or "")
            if row[3] != CHAIN_START_ACTION or row[7] != ZERO_HASH or not match:
                return broken(row[0], "the chain does not begin with a valid start event", checked)
            if match.group(4) != key_fingerprint(key):
                return (
                    CANNOT,
                    "Cannot verify: this key is not the one that started the chain "
                    "(SECRET_KEY or AUDIT_HASH_KEY changed, or the start event was edited). "
                    "Use the original key. This alone is not proof of tampering.",
                    0,
                    None,
                )
            start = (row, match)
        elif row[7] != previous:
            return broken(row[0], "the link to the previous event is broken (an event was removed or inserted)", checked)
        if not hmac.compare_digest(entry_hash(key, row[7], row), row[8] or ""):
            return broken(row[0], "the event content does not match its hash", checked)
        previous = row[8]
        top = row
        checked += 1

    if start is None or top is None or previous is None:
        return broken(None, "the start event is missing", 0)
    if previous != head[0]:
        return broken(top[0], "the stored chain head does not match the last event (events may have been removed)", checked)

    start_row, match = start
    loose = conn.execute(
        "SELECT id FROM audit_log WHERE hash IS NULL AND id > ? ORDER BY id LIMIT 1", (start_row[0],)
    ).fetchone()
    if loose is not None:
        return broken(loose[0], "an event without a hash was added after the chain started", checked)

    count, digest = legacy_digest(conn, start_row[0])
    if count != int(match.group(1)) or digest != match.group(3):
        return broken(None, "events from before the chain were changed or removed", checked)

    if expected_top and not hmac.compare_digest(expected_top.strip().lower(), previous):
        return (
            BROKEN,
            "FAILED: the top of the chain is not the hash you provided. Events were removed, "
            "or the chain was rebuilt, since you copied it.",
            checked,
            previous,
        )
    return OK, f"OK: {checked} chained events verified; the {count} earlier events match their fingerprint.", checked, previous


def read_env_file(path):
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def setting(name, env_file):
    return os.environ.get(name) or env_file.get(name) or ""


def database_path(base, env_file, override):
    if override:
        return Path(override)
    url = setting("DATABASE_URL", env_file)
    if url:
        if not url.startswith("sqlite:///"):
            raise ValueError("Only SQLite databases are supported. Use --db with the file path.")
        path = Path(url[len("sqlite:///"):])
        return path if path.is_absolute() else base / path
    return base / "instance" / "accesskeeper.db"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the AccessKeeper audit log hash chain (read-only).")
    parser.add_argument("--db", help="Path to the SQLite file (default: instance/accesskeeper.db)")
    parser.add_argument("--top", help="Top-of-chain hash you copied earlier, to compare with")
    args = parser.parse_args(argv)

    base = Path(__file__).resolve().parent
    env_file = read_env_file(base / ".env")
    secret = setting("AUDIT_HASH_KEY", env_file) or setting("SECRET_KEY", env_file)
    if len(secret) < 32:
        print("No usable key found. Set SECRET_KEY (or AUDIT_HASH_KEY) in the environment or in .env.")
        return CANNOT
    try:
        db_file = database_path(base, env_file, args.db)
    except ValueError as exc:
        print(exc)
        return CANNOT
    if not db_file.is_file():
        print(f"Database not found: {db_file}")
        return CANNOT

    try:
        conn = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"Could not open the database: {exc}")
        return CANNOT
    try:
        code, message, _checked, top = verify(conn, derive_key(secret), args.top)
    except sqlite3.Error as exc:
        print(f"Could not read the database: {exc}")
        return CANNOT
    finally:
        conn.close()

    print(message)
    if top:
        print(f"Top of the chain: {top}")
    return code


if __name__ == "__main__":
    sys.exit(main())
