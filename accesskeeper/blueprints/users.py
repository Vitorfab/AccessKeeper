from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import select

from .. import audit
from ..extensions import db
from ..forms import ROLE_CHOICES, ResetPasswordForm, UserCreateForm
from ..models import ROLE_ADMIN, ROLE_VIEWER, ROLES, User, master_id
from ..utils import utcnow

bp = Blueprint("users", __name__, url_prefix="/users")


@bp.before_request
def _admin_only():
    if not (current_user.is_authenticated and current_user.is_admin):
        abort(403)


def _back_to_list():
    return redirect(url_for("users.index"))


def _can_manage(actor, target, master):
    if target.id == master:
        return False
    return actor.id == master or target.role == ROLE_VIEWER


def _deny(target, attempted):
    audit.record(
        "user.action_denied",
        target=target.username if target is not None else None,
        details=f"attempted: {attempted}",
    )
    db.session.commit()
    abort(403)


@bp.get("/")
def index():
    users = db.session.execute(select(User).order_by(User.username)).scalars().all()
    master = master_id()
    return render_template(
        "users/list.html",
        users=users,
        roles=ROLES,
        now=utcnow(),
        master=master,
        actor_is_master=current_user.id == master,
        managed={u.id: _can_manage(current_user, u, master) for u in users},
    )


@bp.route("/new", methods=["GET", "POST"])
def create():
    is_master = current_user.id == master_id()
    if request.method == "POST" and not is_master and request.form.get("role") == ROLE_ADMIN:
        _deny(None, "create an administrator")
    form = UserCreateForm()
    if not is_master:
        form.role.choices = [choice for choice in ROLE_CHOICES if choice[0] == ROLE_VIEWER]
    if form.validate_on_submit():
        user = User(username=form.username.data, role=form.role.data)
        user.set_password(form.password.data)
        db.session.add(user)
        audit.record("user.created", target=user.username, details=f"role={user.role}")
        db.session.commit()
        flash(f"User {user.username} created.", "ok")
        return _back_to_list()
    return render_template(
        "users/new.html",
        form=form,
        min_length=current_app.config["MIN_PASSWORD_LENGTH"],
        is_master=is_master,
    )


@bp.post("/<int:user_id>/toggle")
def toggle(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "error")
        return _back_to_list()
    if not _can_manage(current_user, user, master_id()):
        _deny(user, "disable or enable the account")
    user.enabled = not user.enabled
    if user.enabled:
        user.failed_logins = 0
        user.locked_until = None
    else:
        user.session_version += 1
    audit.record("user.enabled" if user.enabled else "user.disabled", target=user.username)
    db.session.commit()
    flash(f"User {user.username} {'enabled' if user.enabled else 'disabled'}.", "ok")
    return _back_to_list()


@bp.post("/<int:user_id>/role")
def change_role(user_id):
    user = db.get_or_404(User, user_id)
    role = request.form.get("role", "")
    if user.id == current_user.id:
        flash("You cannot change your own role.", "error")
        return _back_to_list()
    master = master_id()
    if current_user.id != master and role != user.role:
        _deny(user, "change the role")
    if not _can_manage(current_user, user, master):
        _deny(user, "change the role")
    if role not in ROLES:
        flash("Invalid role.", "error")
    elif role != user.role:
        previous = user.role
        user.role = role
        audit.record("user.role_changed", target=user.username, details=f"{previous} -> {role}")
        db.session.commit()
        flash(f"Role of {user.username} changed to {role}.", "ok")
    return _back_to_list()


@bp.route("/<int:user_id>/reset-password", methods=["GET", "POST"])
def reset_password(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        return redirect(url_for("auth.change_password"))
    if not _can_manage(current_user, user, master_id()):
        _deny(user, "reset the password")
    form = ResetPasswordForm()
    form.context_username = user.username
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.failed_logins = 0
        user.locked_until = None
        audit.record("user.password_reset", target=user.username)
        db.session.commit()
        flash(f"Password for {user.username} was reset.", "ok")
        return _back_to_list()
    return render_template(
        "users/reset.html",
        form=form,
        target_user=user,
        min_length=current_app.config["MIN_PASSWORD_LENGTH"],
    )
