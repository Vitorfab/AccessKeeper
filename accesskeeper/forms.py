import re

from flask import current_app
from flask_wtf import FlaskForm
from sqlalchemy import func, select
from wtforms import (
    DateField,
    DateTimeLocalField,
    EmailField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    EqualTo,
    Length,
    Optional,
    Regexp,
    ValidationError,
)

from .extensions import db
from .models import (
    CRIT_STANDARD,
    CRITICALITIES,
    CRITICALITY_LABELS,
    CRITICALITY_SCOPES,
    OUTCOMES,
    OUTCOME_LABELS,
    ROLE_VIEWER,
    STATUSES,
    STATUS_LABELS,
    USER_TYPES,
    SystemEntry,
    User,
)

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,31}$"
USERNAME_MESSAGE = "3-32 characters: lowercase letters, digits, dot, dash or underscore."
ROLE_CHOICES = [(ROLE_VIEWER, "Viewer (read-only)"), ("admin", "Administrator")]


def _normalize(value):
    return value.strip().lower() if isinstance(value, str) else value


def password_problem(password, username, minimum):
    password = password or ""
    if len(password) < minimum:
        return f"Use at least {minimum} characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    if username and username.lower() in password.lower():
        return "The password must not contain the username."
    return None


def strong_password(form, field):
    username_field = getattr(form, "username", None)
    username = getattr(username_field, "data", None) or getattr(form, "context_username", "")
    problem = password_problem(field.data, username, current_app.config["MIN_PASSWORD_LENGTH"])
    if problem:
        raise ValidationError(problem)


class SetupForm(FlaskForm):
    username = StringField(
        "Username",
        filters=[_normalize],
        validators=[DataRequired(), Regexp(USERNAME_PATTERN, message=USERNAME_MESSAGE)],
    )
    password = PasswordField("Password", validators=[DataRequired(), strong_password])
    confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")],
    )


class LoginForm(FlaskForm):
    username = StringField(
        "Username", filters=[_normalize], validators=[DataRequired(), Length(max=64)]
    )
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])


class UserCreateForm(SetupForm):
    role = SelectField("Role", choices=ROLE_CHOICES, default=ROLE_VIEWER)

    def validate_username(self, field):
        taken = db.session.execute(
            select(User.id).filter_by(username=field.data)
        ).first()
        if taken:
            raise ValidationError("That username is already taken.")


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")],
    )


class ChangePasswordForm(ResetPasswordForm):
    current = PasswordField("Current password", validators=[DataRequired(), Length(max=128)])


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"]
USER_TYPE_CHOICES = [("internal", "Internal"), ("external", "External")]
CRITICALITY_CHOICES = [(level, CRITICALITY_LABELS[level]) for level in CRITICALITIES]


def _single_line(value):
    if not isinstance(value, str):
        return value
    return " ".join(_CONTROL_CHARS.sub("", value).split())


def _multi_line(value):
    if not isinstance(value, str):
        return value
    return _CONTROL_CHARS.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _lower(value):
    return value.lower() if isinstance(value, str) else value


def reasonable_date(form, field):
    if field.data is not None and not 2000 <= field.data.year <= 2100:
        raise ValidationError("Use a date between 2000 and 2100.")


def _text(label, limit, required=True):
    validators = [DataRequired() if required else Optional(), Length(max=limit)]
    return StringField(label, filters=[_single_line], validators=validators)


def _area(label, limit, required=True):
    validators = [DataRequired() if required else Optional(), Length(max=limit)]
    return TextAreaField(label, filters=[_multi_line], validators=validators)


def _moment(label):
    return DateTimeLocalField(
        label, format=DATETIME_FORMATS, validators=[DataRequired(), reasonable_date]
    )


class AccessForm(FlaskForm):
    name = _text("Full name", 120)
    email = EmailField(
        "Email",
        filters=[_single_line, _lower],
        validators=[
            DataRequired(),
            Length(max=254),
            Regexp(EMAIL_PATTERN, message="Enter a valid email address."),
        ],
    )
    company = _text("Company", 120)
    user_type = SelectField("User type", choices=USER_TYPE_CHOICES, validators=[DataRequired()])
    access_type = _text("Access type", 120)
    system = SelectField("System / target", choices=[], validators=[DataRequired()])
    criticality = SelectField(
        "Criticality",
        choices=CRITICALITY_CHOICES,
        default=CRIT_STANDARD,
        validators=[DataRequired()],
    )
    description = _area("Description", 1000)
    justification = _area("Justification", 1000)
    authorized_by = _text("Authorized by", 120)
    ticket = _text("Ticket number (optional)", 64, required=False)
    start_at = _moment("Start")
    notes = _area("Notes (optional)", 2000, required=False)


class AccessCreateForm(AccessForm):
    end_at = _moment("End")

    def validate_end_at(self, field):
        start = self.start_at.data
        if start is not None and field.data is not None and field.data <= start:
            raise ValidationError("The end must be after the start.")


class AccessEditForm(AccessForm):
    pass


class RenewForm(FlaskForm):
    new_end_at = _moment("New end")
    reason = _area("Reason for renewal", 300)


class CloseForm(FlaskForm):
    reason = TextAreaField(
        "Reason for closing",
        filters=[_multi_line],
        validators=[DataRequired(), Length(min=3, max=300)],
    )


class CampaignForm(FlaskForm):
    name = _text("Campaign name", 120)
    start_date = DateField("Start date", validators=[DataRequired(), reasonable_date])
    due_date = DateField("Due date", validators=[DataRequired(), reasonable_date])
    status = SelectField(
        "Status",
        choices=[("", "All except closed")]
        + [(s, STATUS_LABELS[s]) for s in STATUSES],
    )
    user_type = SelectField(
        "User type",
        choices=[("", "Internal and external")] + [(t, t.capitalize()) for t in USER_TYPES],
    )
    criticality = SelectField(
        "Criticality",
        choices=[("", "Any criticality")] + list(CRITICALITY_SCOPES.items()),
        default="",
    )
    company = SelectField("Company", choices=[("", "Any company")])
    system = SelectField("System / target", choices=[("", "Any system")])

    def validate_due_date(self, field):
        start = self.start_date.data
        if start is not None and field.data is not None and field.data < start:
            raise ValidationError("The due date cannot be before the start date.")


class ReviewForm(FlaskForm):
    outcome = SelectField("Outcome", validators=[DataRequired()])
    justification = TextAreaField(
        "Justification",
        filters=[_multi_line],
        validators=[Length(max=500)],
    )

    def validate_justification(self, field):
        if self.outcome.data == "kept" and len(field.data or "") < 3:
            raise ValidationError("A justification is required to keep an access.")


class BulkReviewForm(FlaskForm):
    outcome = SelectField(
        "Outcome",
        choices=[("", "Choose an outcome")] + [(o, OUTCOME_LABELS[o]) for o in OUTCOMES],
        validators=[DataRequired(message="Choose an outcome.")],
    )
    justification = TextAreaField(
        "Justification",
        filters=[_multi_line],
        validators=[Length(max=500)],
    )


class SystemForm(FlaskForm):
    name = _text("System name", 120)

    def validate_name(self, field):
        taken = db.session.scalar(
            select(func.count(SystemEntry.id)).where(func.lower(SystemEntry.name) == field.data.lower())
        )
        if taken:
            raise ValidationError("That system is already in the catalog.")
