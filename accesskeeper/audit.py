import re

from flask import has_request_context, request
from flask_login import current_user

from . import integrity
from .extensions import db
from .models import AuditLog
from .utils import utcnow

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value, limit):
    if value is None:
        return None
    return _CONTROL_CHARS.sub(" ", str(value))[:limit]


def record(action, target=None, details=None, actor=None):
    if actor is None:
        actor = current_user.username if current_user.is_authenticated else "anonymous"
    ip = request.remote_addr if has_request_context() else "local"
    row = AuditLog(
        timestamp=utcnow(),
        actor=_clean(actor, 64),
        action=_clean(action, 48),
        target=_clean(target, 200),
        details=_clean(details, 500),
        ip=_clean(ip, 45),
    )
    integrity.seal(row)
    db.session.add(row)
