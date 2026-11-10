"""Management command to enroll staff users, generate temporary passwords, and send invitations."""

import csv
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from requisitions.enrollment import enroll_user, enroll_users_from_csv
from requisitions.models import User


class Command(BaseCommand):
    help = (
        "Enroll a single staff member or a CSV roster of users with temporary credentials, "
        "send invitation emails, and enforce a first-login password reset."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            dest="csv_path",
            help="Path to a CSV file with columns: username,email,first_name,last_name,role,department",
        )
        parser.add_argument(
            "--username",
            help="Username for enrolling a single user",
        )
        parser.add_argument(
            "--email",
            default="",
            help="Email address for the user",
        )
        parser.add_argument(
            "--first-name",
            default="",
            help="First name for the user",
        )
        parser.add_argument(
            "--last-name",
            default="",
            help="Last name for the user",
        )
        parser.add_argument(
            "--role",
            default=User.ROLE_EMPLOYEE,
            help=(
                "Workflow role: EMPLOYEE, SUPERVISOR, ME_OFFICER, PROGRAM_LEAD, "
                "EXECUTIVE_DIRECTOR, FINANCE, INTERNAL_AUDIT, or ADMIN"
            ),
        )
        parser.add_argument(
            "--department",
            default="",
            help="Department name (e.g., Programs, Finance)",
        )
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Do not send invitation emails; only print/export temporary credentials",
        )
        parser.add_argument(
            "--reset-existing",
            action="store_true",
            help="If a username already exists, update the account and issue a new temporary password",
        )
        parser.add_argument(
            "--login-url",
            default=None,
            help="Override the sign-in URL included in invitation emails",
        )
        parser.add_argument(
            "--output-credentials",
            dest="output_path",
            help="Optional path to write a CSV file containing the generated temporary credentials",
        )

    def handle(self, *args, **options):
        csv_path = options.get("csv_path")
        username = options.get("username")
        send_invite = not options.get("no_email", False)
        reset_existing = options.get("reset_existing", False)
        login_url = options.get("login_url")
        output_path = options.get("output_path")

        if bool(csv_path) == bool(username):
            raise CommandError("Specify either --csv <path> or --username <username> (not both).")

        results = []
        errors = []

        if csv_path:
            path = Path(csv_path)
            if not path.is_file():
                raise CommandError(f"CSV file not found: {csv_path}")
            results, errors = enroll_users_from_csv(
                path.read_bytes(),
                send_invite=send_invite,
                reset_existing=reset_existing,
                login_url=login_url,
            )
        else:
            try:
                result = enroll_user(
                    username=username,
                    email=options.get("email", ""),
                    first_name=options.get("first_name", ""),
                    last_name=options.get("last_name", ""),
                    role=options.get("role", User.ROLE_EMPLOYEE),
                    department=options.get("department", ""),
                    send_invite=send_invite,
                    reset_existing=reset_existing,
                    login_url=login_url,
                )
                results.append(result)
            except ValidationError as exc:
                raise CommandError("; ".join(exc.messages)) from exc

        for err in errors:
            self.stderr.write(self.style.ERROR(err))

        if results:
            self.stdout.write(
                self.style.SUCCESS(f"Enrolled {len(results)} user account(s):")
            )
            header = f"{'USERNAME':<18} {'ROLE':<20} {'EMAIL':<28} {'TEMP PASSWORD':<18} {'INVITATION'}"
            self.stdout.write(header)
            self.stdout.write("-" * len(header))
            for item in results:
                if item.email_sent:
                    invite_status = "Sent"
                elif item.email_error:
                    invite_status = f"Failed ({item.email_error})"
                else:
                    invite_status = "Skipped"
                self.stdout.write(
                    f"{item.user.username:<18} "
                    f"{item.user.role:<20} "
                    f"{(item.user.email or '-'):<28} "
                    f"{item.temporary_password:<18} "
                    f"{invite_status}"
                )

            if output_path:
                out_file = Path(output_path)
                with out_file.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(
                        [
                            "username",
                            "email",
                            "first_name",
                            "last_name",
                            "role",
                            "department",
                            "temporary_password",
                            "email_sent",
                        ]
                    )
                    for item in results:
                        writer.writerow(
                            [
                                item.user.username,
                                item.user.email,
                                item.user.first_name,
                                item.user.last_name,
                                item.user.role,
                                item.user.department,
                                item.temporary_password,
                                item.email_sent,
                            ]
                        )
                self.stdout.write(
                    self.style.SUCCESS(f"Saved generated credentials to {out_file}")
                )

        if errors and not results:
            raise CommandError("Enrollment failed due to validation errors.")
