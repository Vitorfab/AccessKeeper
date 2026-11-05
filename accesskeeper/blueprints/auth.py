from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, select
from werkzeug.security import check_password_hash

from .. import audit
from ..extensions import db
from ..forms import ChangePasswordForm, LoginForm, SetupForm
from ..models import DUMMY_HASH, ROLE_ADMIN, User
from ..utils import is_safe_next, utcnow

bp = Blueprint("auth", __name__)

GENERIC_LOGIN_ERROR = "Invalid username or password, or the account is unavailable."


def _has_users():
    return db.session.scalar(select(func.count(User.id))) > 0


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if _has_users():
        return redirect(url_for("auth.login"))
    form = SetupForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, role=ROLE_ADMIN)
        user.set_password(form.password.data)
        db.session.add(user)
        audit.record("setup.admin_created", target=user.username, actor=user.username)
        db.session.commit()
        flash("Administrator created. Please sign in.", "ok")
        return redirect(url_for("auth.login"))
    return render_template(
        "setup.html", form=form, min_length=current_app.config["MIN_PASSWORD_LENGTH"]
    )


def _check_credentials(user, password):
    if user is None:
        check_password_hash(DUMMY_HASH, password)
        return "unknown_user", False
    password_ok = user.check_password(password)
    if not user.enabled:
        return "disabled", False
    if user.is_locked(utcnow()):
        return "locked", False
    if not password_ok:
        locked_now = user.register_failure(
            current_app.config["MAX_LOGIN_ATTEMPTS"],
            current_app.config["LOCKOUT_MINUTES"],
        )
        return "bad_password", locked_now
    return None, False


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    form = LoginForm()
    if request.method == "GET" and request.args.get("timeout") == "1":
        minutes = current_app.config["IDLE_TIMEOUT_MINUTES"]
        flash(
            f"You were signed out after {minutes} minute{'s' if minutes != 1 else ''} without activity.",
            "info",
        )
    if form.validate_on_submit():
        username = form.username.data
        user = db.session.execute(select(User).filter_by(username=username)).scalar_one_or_none()
        failure, locked_now = _check_credentials(user, form.password.data)
        if failure is None:
            user.register_success()
            session.clear()
            login_user(user)
            session.permanent = True
            audit.record("auth.login_success", target=user.username, actor=user.username)
            db.session.commit()
            target = request.args.get("next")
            return redirect(target if is_safe_next(target) else url_for("main.index"))
        audit.record("auth.login_failure", details=failure, actor="anonymous")
        if locked_now:
            audit.record("auth.account_locked", target=user.username, actor="system")
        db.session.commit()
        flash(GENERIC_LOGIN_ERROR, "error")
    return render_template("login.html", form=form)


@bp.post("/session/ping")
def session_ping():
    return "", 204


@bp.post("/session/expire")
def session_expire():
    if current_user.is_authenticated:
        audit.record("auth.session_timeout", target=current_user.username)
        db.session.commit()
        logout_user()
        session.clear()
    return "", 204


@bp.post("/logout")
@login_required
def logout():
    audit.record("auth.logout", target=current_user.username)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "ok")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user._get_current_object()
    form = ChangePasswordForm()
    form.context_username = user.username
    if form.validate_on_submit():
        if not user.check_password(form.current.data):
            form.current.errors = list(form.current.errors) + ["Current password is incorrect."]
        else:
            user.set_password(form.password.data)
            audit.record("user.password_changed", target=user.username)
            db.session.commit()
            login_user(user)
            session.permanent = True
            flash("Password updated.", "ok")
            return redirect(url_for("main.index"))
    return render_template(
        "account/password.html", form=form, min_length=current_app.config["MIN_PASSWORD_LENGTH"]
    )
