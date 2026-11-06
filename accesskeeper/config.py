import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name, default):
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except ValueError:
        return default


def _env_choice(name, default, choices):
    value = _env(name, default)
    return value if value in choices else default


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = _env("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = _env("DATABASE_URL", "") or None
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    TIMEZONE = _env("TIMEZONE", "America/Sao_Paulo")
    HOST = _env("HOST", "127.0.0.1")
    PORT = _env_int("PORT", 5000)

    EXPIRING_DAYS = max(1, _env_int("EXPIRING_DAYS", 14))
    CSV_DELIMITER = _env_choice("CSV_DELIMITER", ",", (",", ";"))

    CRITICAL_MAX_DAYS = max(1, _env_int("CRITICAL_MAX_DAYS", 30))
    HIGH_MAX_DAYS = max(1, _env_int("HIGH_MAX_DAYS", 90))

    AUDIT_HASH_KEY = _env("AUDIT_HASH_KEY", "")
    INTEGRITY_REMINDER_DAYS = max(0, _env_int("INTEGRITY_REMINDER_DAYS", 30))

    MIN_PASSWORD_LENGTH = max(8, _env_int("MIN_PASSWORD_LENGTH", 12))
    MAX_LOGIN_ATTEMPTS = _env_int("MAX_LOGIN_ATTEMPTS", 5)
    LOCKOUT_MINUTES = _env_int("LOCKOUT_MINUTES", 15)

    SESSION_COOKIE_NAME = "ak_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("COOKIE_SECURE", False)
    IDLE_TIMEOUT_MINUTES = max(1, _env_int("IDLE_TIMEOUT_MINUTES", 5))
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=IDLE_TIMEOUT_MINUTES, seconds=20)

    WTF_CSRF_TIME_LIMIT = None
    MAX_CONTENT_LENGTH = 1024 * 1024
