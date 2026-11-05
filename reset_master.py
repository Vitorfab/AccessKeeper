import getpass
import sys

from accesskeeper import audit, create_app
from accesskeeper.extensions import db
from accesskeeper.forms import password_problem
from accesskeeper.models import User, master_id

ACTOR = "local-console"


def main():
    app = create_app()
    with app.app_context():
        user_id = master_id()
        master = db.session.get(User, user_id) if user_id else None
        if master is None:
            print("There are no users yet. Start the app and complete the setup page first.")
            return 1

        print(f"Master account: {master.username}")
        print("This sets a new password, unlocks and enables the account, and signs it out everywhere.")
        if input("Type RESET to continue: ").strip() != "RESET":
            print("Cancelled. Nothing was changed.")
            return 1

        minimum = app.config["MIN_PASSWORD_LENGTH"]
        password = getpass.getpass("New password: ")
        problem = password_problem(password, master.username, minimum)
        if problem:
            print(problem)
            return 1
        if getpass.getpass("Confirm new password: ") != password:
            print("Passwords do not match. Nothing was changed.")
            return 1

        master.set_password(password)
        master.enabled = True
        master.failed_logins = 0
        master.locked_until = None
        audit.record(
            "user.master_password_reset_local",
            target=master.username,
            details="via reset_master.py",
            actor=ACTOR,
        )
        db.session.commit()
        print("Done. The master password was changed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
