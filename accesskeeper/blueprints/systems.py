from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user
from sqlalchemy import func, select

from .. import audit, services
from ..extensions import db
from ..forms import SystemForm
from ..models import SystemEntry
from ..permissions import admin_required

bp = Blueprint("systems", __name__, url_prefix="/systems")


@bp.route("/", methods=["GET", "POST"])
@admin_required
def index():
    form = SystemForm()
    if form.validate_on_submit():
        entry = SystemEntry(name=form.name.data, created_by=current_user.username)
        db.session.add(entry)
        audit.record("system.created", target=entry.name)
        db.session.commit()
        flash(f"System {entry.name} added to the catalog.", "ok")
        return redirect(url_for("systems.index"))
    entries = db.session.scalars(select(SystemEntry).order_by(func.lower(SystemEntry.name))).all()
    return render_template("systems/list.html", form=form, entries=entries, usage=services.system_usage())


@bp.post("/<int:system_id>/toggle")
@admin_required
def toggle(system_id):
    entry = db.get_or_404(SystemEntry, system_id)
    entry.enabled = not entry.enabled
    audit.record("system.enabled" if entry.enabled else "system.disabled", target=entry.name)
    db.session.commit()
    flash(f"System {entry.name} {'enabled' if entry.enabled else 'disabled'}.", "ok")
    return redirect(url_for("systems.index"))
