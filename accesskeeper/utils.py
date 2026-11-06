from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import current_app

INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tz():
    return ZoneInfo(current_app.config["TIMEZONE"])


def to_local(value):
    return value.replace(tzinfo=timezone.utc).astimezone(_tz()).replace(tzinfo=None)


def to_utc(value):
    return value.replace(tzinfo=_tz()).astimezone(timezone.utc).replace(tzinfo=None)


def local_dt(value, fmt="%Y-%m-%d %H:%M"):
    if value is None:
        return ""
    return to_local(value).strftime(fmt)


def local_input(value):
    return local_dt(value, INPUT_FORMAT)


def utc_text(value):
    return value.strftime("%Y-%m-%d %H:%M UTC")


def humanize_delta(value, now=None):
    if value is None:
        return ""
    seconds = int((value - (now or utcnow())).total_seconds())
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "less than a minute"
    elif seconds < 3600:
        text = f"{seconds // 60} min"
    elif seconds < 86400:
        text = f"{seconds // 3600} h"
    else:
        days = seconds // 86400
        text = f"{days} day{'s' if days != 1 else ''}"
    return f"in {text}" if future else f"{text} ago"


def is_safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target or any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc
