import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from flask import current_app
from sqlalchemy import func, select

from .extensions import db
from .models import AuditLog, audit_chain
from .utils import utcnow

ZERO_HASH = "0" * 64
CHAIN_START_ACTION = "audit.chain_started"
CHECK_ACTION = "audit.integrity_checked"
_KEY_INFO = b"accesskeeper audit log hash chain v1"
_START_DETAILS = re.compile(
    r"legacy_events=(\d+) \| last_legacy_id=(\d+) \| legacy_digest=([0-9a-f]{64}) \| key_fp=([0-9a-f]{16})"
)
_BATCH = 1000


def derive_key(secret):
    """HKDF-SHA256 (RFC 5869) with a purpose label, so the chain key is never the raw secret."""
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, _KEY_INFO + b"\x01", hashlib.sha256).digest()


def key_fingerprint(key):
    return hmac.new(key, b"accesskeeper audit key fingerprint", hashlib.sha256).hexdigest()[:16]


def _key():
    return current_app.config["AUDIT_KEY"]


def _stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _canonical(values):
    return json.dumps(values, separators=(",", ":")).encode("utf-8")


def entry_hash(key, prev_hash, row):
    payload = _canonical(
        [prev_hash, _stamp(row.timestamp), row.actor, row.action, row.target, row.details, row.ip]
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def seal(row):
    """Chain a new audit row to the previous one.

    The first statement writes to the chain head, which takes SQLite's write lock for the
    rest of the transaction. Reading the head after that is safe: no other request can
    chain an event until this transaction ends, so two events never share a previous hash.
    """
    head = audit_chain.c
    db.session.execute(audit_chain.update().where(head.id == 1).values(last_hash=head.last_hash))
    previous = db.session.execute(select(head.last_hash).where(head.id == 1)).scalar_one_or_none()
    if previous is None:
        raise RuntimeError("The audit hash chain was not initialised.")
    row.prev_hash = previous
    row.hash = entry_hash(_key(), previous, row)
    db.session.execute(audit_chain.update().where(head.id == 1).values(last_hash=row.hash))


def _legacy_rows(before_id=None):
    last_id = 0
    while True:
        stmt = select(AuditLog).where(AuditLog.hash.is_(None), AuditLog.id > last_id)
        if before_id is not None:
            stmt = stmt.where(AuditLog.id < before_id)
        batch = db.session.scalars(stmt.order_by(AuditLog.id).limit(_BATCH)).all()
        if not batch:
            return
        yield from batch
        last_id = batch[-1].id


def _legacy_digest(rows):
    digest = hashlib.sha256()
    count = last_id = 0
    for row in rows:
        values = [row.id, _stamp(row.timestamp), row.actor, row.action, row.target, row.details, row.ip]
        digest.update(_canonical(values) + b"\n")
        count += 1
        last_id = row.id
    return count, last_id, digest.hexdigest()


def start_chain():
    """Begin the chain once. Events recorded before it are fingerprinted, never rewritten."""
    if db.session.execute(select(audit_chain.c.id)).first() is not None:
        return
    count, last_id, digest = _legacy_digest(_legacy_rows())
    db.session.execute(audit_chain.insert().values(id=1, last_hash=ZERO_HASH))
    row = AuditLog(
        timestamp=utcnow(),
        actor="system",
        action=CHAIN_START_ACTION,
        details=(
            f"legacy_events={count} | last_legacy_id={last_id} | legacy_digest={digest}"
            f" | key_fp={key_fingerprint(_key())}"
        ),
        ip="local",
    )
    seal(row)
    db.session.add(row)


def _chained_rows():
    last_id = 0
    while True:
        batch = db.session.scalars(
            select(AuditLog)
            .where(AuditLog.hash.is_not(None), AuditLog.id > last_id)
            .order_by(AuditLog.id)
            .limit(_BATCH)
        ).all()
        if not batch:
            return
        yield from batch
        last_id = batch[-1].id


def summary():
    head = db.session.execute(select(audit_chain.c.last_hash)).scalar_one_or_none()
    if head is None:
        return {"started": False}
    chained = AuditLog.hash.is_not(None)
    first = db.session.scalars(select(AuditLog).where(chained).order_by(AuditLog.id).limit(1)).first()
    top = db.session.scalars(select(AuditLog).where(chained).order_by(AuditLog.id.desc()).limit(1)).first()
    match = _START_DETAILS.fullmatch(first.details or "") if first is not None else None
    return {
        "started": True,
        "chained": db.session.scalar(select(func.count(AuditLog.id)).where(chained)),
        "legacy": db.session.scalar(select(func.count(AuditLog.id)).where(AuditLog.hash.is_(None))),
        "top": top,
        "started_at": first.timestamp if first is not None else None,
        "key_matches": bool(match) and match.group(4) == key_fingerprint(_key()),
    }


def last_check():
    return db.session.scalars(
        select(AuditLog).where(AuditLog.action == CHECK_ACTION).order_by(AuditLog.id.desc()).limit(1)
    ).first()


def reminder(now, days):
    """None when nothing is due; otherwise how long the audit log has gone unverified."""
    if days <= 0 or db.session.execute(select(audit_chain.c.id)).first() is None:
        return None
    last = last_check()
    if last is None:
        return {"never": True, "days": None}
    age = (now - last.timestamp).days
    return {"never": False, "days": age} if age >= days else None


@dataclass
class Verification:
    status: str
    message: str
    checked: int = 0
    legacy: int = 0
    broken_id: int = None


def _broken(row_id, reason, checked):
    where = f" at event #{row_id}" if row_id else ""
    return Verification(
        "broken",
        f"Integrity check FAILED{where}: {reason}. Events from there on cannot be trusted.",
        checked=checked,
        broken_id=row_id,
    )


def verify():
    key = _key()
    head = db.session.execute(select(audit_chain.c.last_hash)).scalar_one_or_none()
    if head is None:
        return Verification("not_started", "The hash chain has not been started yet.")

    previous = start = top = None
    checked = 0
    for row in _chained_rows():
        if previous is None:
            match = _START_DETAILS.fullmatch(row.details or "")
            if row.action != CHAIN_START_ACTION or row.prev_hash != ZERO_HASH or not match:
                return _broken(row.id, "the chain does not begin with a valid start event", checked)
            if match.group(4) != key_fingerprint(key):
                return Verification(
                    "key_changed",
                    "Cannot verify: the audit key is not the one that started the chain "
                    "(SECRET_KEY or AUDIT_HASH_KEY changed, or the start event was edited). "
                    "Restore the original key and try again. This alone is not proof of tampering.",
                )
            start = (row, match)
        elif row.prev_hash != previous:
            return _broken(row.id, "the link to the previous event is broken (an event was removed or inserted)", checked)
        if not hmac.compare_digest(entry_hash(key, row.prev_hash, row), row.hash or ""):
            return _broken(row.id, "the event content does not match its hash", checked)
        previous = row.hash
        top = row
        checked += 1

    if start is None:
        return _broken(None, "the start event is missing", 0)
    if previous != head:
        return _broken(top.id, "the stored chain head does not match the last event (events may have been removed)", checked)

    start_row, match = start
    loose = db.session.scalar(
        select(AuditLog.id).where(AuditLog.hash.is_(None), AuditLog.id > start_row.id).order_by(AuditLog.id).limit(1)
    )
    if loose is not None:
        return _broken(loose, "an event without a hash was added after the chain started", checked)

    count, _last, digest = _legacy_digest(_legacy_rows(before_id=start_row.id))
    if count != int(match.group(1)) or digest != match.group(3):
        return _broken(None, "events from before the chain were changed or removed", checked)

    return Verification(
        "ok",
        f"Integrity check passed: {checked} chained events verified, and the {count} earlier events "
        "still match their recorded fingerprint.",
        checked=checked,
        legacy=count,
    )
