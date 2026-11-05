import os
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from .config import Config
from .extensions import csrf, db, login_manager
from .utils import humanize_delta, local_dt

PUBLIC_ENDPOINTS = {"auth.login", "auth.setup", "static"}
SESSION_ENDPOINTS = {"auth.session_ping", "auth.session_expire"}

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    )
)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(overrides=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)

    if len(app.config.get("SECRET_KEY") or "") < 32:
        raise RuntimeError(
            "SECRET_KEY is missing or too short. Restore your .env backup if you have one. "
            "Otherwise delete the .env file and start again with run.bat so a new one is generated "
            "(the audit log integrity check will then report that the key changed)."
        )
    audit_secret = app.config.get("AUDIT_HASH_KEY") or ""
    if audit_secret and len(audit_secret) < 32:
        raise RuntimeError("AUDIT_HASH_KEY must have at least 32 characters, or be left empty.")
    try:
        ZoneInfo(app.config["TIMEZONE"])
    except Exception as exc:
        raise RuntimeError(f"Unknown TIMEZONE '{app.config['TIMEZONE']}' in .env.") from exc

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        db_file = Path(app.instance_path) / "accesskeeper.db"
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_file.as_posix()}"

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.session_protection = "strong"

    from . import integrity, migrations, models  # noqa: F401  (registers tables)

    app.config["AUDIT_KEY"] = integrity.derive_key(audit_secret or app.config["SECRET_KEY"])
    from .blueprints import access, auth, main, review, systems, users

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(access.bp)
    app.register_blueprint(review.bp)
    app.register_blueprint(systems.bp)
    app.add_template_filter(local_dt, "local")
    app.add_template_filter(humanize_delta, "reltime")
    app.jinja_env.globals.update(
        STATUS_LABELS=models.STATUS_LABELS,
        STATUS_BADGES=models.STATUS_BADGES,
        CAMPAIGN_LABELS=models.CAMPAIGN_LABELS,
        CAMPAIGN_BADGES=models.CAMPAIGN_BADGES,
        CRITICALITY_LABELS=models.CRITICALITY_LABELS,
        CRITICALITY_BADGES=models.CRITICALITY_BADGES,
        OUTCOME_LABELS=models.OUTCOME_LABELS,
    )

    @app.after_request
    def security_headers(response):
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.endpoint != "static":
            headers.setdefault("Cache-Control", "no-store")
        if app.config.get("SESSION_COOKIE_SECURE"):
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.context_processor
    def idle_settings():
        return {"idle_seconds": app.config["IDLE_TIMEOUT_MINUTES"] * 60}

    @app.before_request
    def require_login():
        endpoint = request.endpoint
        if endpoint in SESSION_ENDPOINTS and not current_user.is_authenticated:
            return Response(status=401)
        if endpoint is None or endpoint == "static" or current_user.is_authenticated:
            return None
        has_users = db.session.scalar(select(func.count(models.User.id))) > 0
        if not has_users:
            return None if endpoint == "auth.setup" else redirect(url_for("auth.setup"))
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if request.method == "GET":
            target = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login", next=target))
        return redirect(url_for("auth.login"))

    def error_page(code, message):
        return render_template("error.html", code=code, message=message), code

    @app.errorhandler(400)
    def bad_request(_error):
        return error_page(400, "The request could not be processed.")

    @app.errorhandler(CSRFError)
    def csrf_failed(_error):
        return error_page(400, "Your form expired or was invalid. Reload the page and try again.")

    @app.errorhandler(403)
    def forbidden(_error):
        return error_page(403, "You do not have permission to do that.")

    @app.errorhandler(404)
    def not_found(_error):
        return error_page(404, "That page does not exist.")

    @app.errorhandler(413)
    def too_large(_error):
        return error_page(413, "The request is too large.")

    with app.app_context():
        db.create_all()
        migrations.run()

    return app
