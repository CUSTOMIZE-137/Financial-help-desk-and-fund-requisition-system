"""User enrollment service for generating temporary credentials and sending invitations."""

import csv
from dataclasses import dataclass
import io
import secrets
import string
from typing import Iterable

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.urls import reverse

from requisitions.models import User

_SYMBOLS = "!@#$%^&*-_=+"
_SYSRAND = secrets.SystemRandom()


@dataclass
class EnrollmentResult:
    """Summary of a single enrolled or re-invited user account."""

    user: User
    temporary_password: str
    created: bool
    email_sent: bool
    email_error: str = ""


def generate_temporary_password(length: int = 14, user: User | None = None) -> str:
    """Generate a cryptographically random temporary password that satisfies Django validators."""
    if length < 12:
        length = 12
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    # Guarantee at least one character from each character class (upper, lower, digit, symbol)
    # and validate against Django's configured AUTH_PASSWORD_VALIDATORS before returning.
    for _ in range(25):
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(_SYMBOLS),
        ]
        chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
        _SYSRAND.shuffle(chars)
        candidate = "".join(chars)
        try:
            validate_password(candidate, user=user)
            return candidate
        except ValidationError:
            continue
    return candidate


def resolve_login_url(request=None, login_url: str | None = None) -> str:
    """Resolve the absolute sign-in URL for invitation emails."""
    if login_url:
        return login_url
    login_path = reverse("login")
    if request is not None:
        return request.build_absolute_uri(login_path)
    site_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{site_url}{login_path}"


def send_enrollment_invitation(
    user: User,
    temporary_password: str,
    *,
    request=None,
    login_url: str | None = None,
) -> tuple[bool, str]:
    """Send an account enrollment email containing temporary sign-in credentials."""
    if not user.email:
        return False, "No email address configured for this user."

    resolved_url = resolve_login_url(request=request, login_url=login_url)
    display_name = user.get_full_name() or user.username
    department_line = f"Department: {user.department}\n" if user.department else ""

    subject = "You have been invited to FinanceDesk (Financial Help Desk & Fund Requisition System)"
    message = (
        f"Hello {display_name},\n\n"
        "An administrator has enrolled your account in the Financial Help Desk & Fund Requisition System (FinanceDesk).\n\n"
        "Your account details:\n"
        f"Username: {user.username}\n"
        f"Temporary Password: {temporary_password}\n"
        f"Assigned Role: {user.get_role_display()}\n"
        f"{department_line}"
        f"\nSign in here: {resolved_url}\n\n"
        "IMPORTANT: For your security, this temporary password is valid only for your initial sign-in. "
        "You will be prompted to set a new personal password immediately after signing in.\n\n"
        "Regards,\n"
        "FinanceDesk System Administration"
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@financedesk.local")
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[user.email],
            fail_silently=False,
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _dispatch_invitation(
    user: User,
    temporary_password: str,
    *,
    request=None,
    login_url: str | None = None,
) -> tuple[bool, str]:
    """Send or enqueue the enrollment invitation depending on BACKGROUND_TASK_MODE."""
    mode = getattr(settings, "BACKGROUND_TASK_MODE", "eager").strip().lower()
    if mode in ("db_queue", "thread"):
        from requisitions.tasks import enqueue_task

        resolved_url = resolve_login_url(request=request, login_url=login_url)
        enqueue_task(
            "send_enrollment_invitation",
            user_id=user.pk,
            temporary_password=temporary_password,
            login_url=resolved_url,
        )
        return True, ""
    return send_enrollment_invitation(
        user, temporary_password, request=request, login_url=login_url
    )


def enroll_user(
    *,
    username: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    role: str = User.ROLE_EMPLOYEE,
    department: str = "",
    send_invite: bool = True,
    reset_existing: bool = False,
    request=None,
    login_url: str | None = None,
) -> EnrollmentResult:
    """Create or re-enroll a user with a temporary password and mandatory first-login reset."""
    username = (username or "").strip()
    email = (email or "").strip()
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    role = (role or User.ROLE_EMPLOYEE).strip().upper()
    department = (department or "").strip()

    if not username:
        raise ValidationError({"username": "Username is required."})

    valid_roles = dict(User.ROLE_CHOICES)
    if role not in valid_roles:
        # Also allow matching by display label (e.g. "Program Lead" or "Employee")
        label_to_code = {label.upper(): code for code, label in User.ROLE_CHOICES}
        if role in label_to_code:
            role = label_to_code[role]
        else:
            allowed = ", ".join(valid_roles.keys())
            raise ValidationError({"role": f"Invalid role '{role}'. Allowed values: {allowed}."})

    existing_user = User.objects.filter(username=username).first()
    if existing_user and not reset_existing:
        raise ValidationError({"username": f"A user with username '{username}' already exists."})

    created = existing_user is None
    user = existing_user or User(username=username)
    user.email = email or user.email
    if first_name or created:
        user.first_name = first_name
    if last_name or created:
        user.last_name = last_name
    user.role = role
    if department or created:
        user.department = department
    user.is_active = True
    user.must_change_password = True

    temp_password = generate_temporary_password(user=user)
    user.set_password(temp_password)
    user.full_clean()
    user.save()

    email_sent = False
    email_error = ""
    if send_invite and user.email:
        email_sent, email_error = _dispatch_invitation(
            user, temp_password, request=request, login_url=login_url
        )

    return EnrollmentResult(
        user=user,
        temporary_password=temp_password,
        created=created,
        email_sent=email_sent,
        email_error=email_error,
    )


def issue_temporary_credentials(
    user: User,
    *,
    send_invite: bool = True,
    request=None,
    login_url: str | None = None,
) -> EnrollmentResult:
    """Generate a new temporary password for an existing user and require password reset on next login."""
    temp_password = generate_temporary_password(user=user)
    user.set_password(temp_password)
    user.must_change_password = True
    user.is_active = True
    user.save(update_fields=["password", "must_change_password", "is_active"])

    email_sent = False
    email_error = ""
    if send_invite and user.email:
        email_sent, email_error = _dispatch_invitation(
            user, temp_password, request=request, login_url=login_url
        )

    return EnrollmentResult(
        user=user,
        temporary_password=temp_password,
        created=False,
        email_sent=email_sent,
        email_error=email_error,
    )


def enroll_users_from_csv(
    csv_source: str | bytes | Iterable[str],
    *,
    send_invite: bool = True,
    reset_existing: bool = False,
    request=None,
    login_url: str | None = None,
) -> tuple[list[EnrollmentResult], list[str]]:
    """Parse a CSV stream of users and enroll each row, collecting results and row-level errors."""
    if isinstance(csv_source, bytes):
        text = csv_source.decode("utf-8-sig")
        stream = io.StringIO(text)
    elif isinstance(csv_source, str):
        stream = io.StringIO(csv_source)
    else:
        stream = csv_source

    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        return [], ["The CSV file is empty or missing a header row."]

    normalized_headers = {h.strip().lower(): h for h in reader.fieldnames if h}
    if "username" not in normalized_headers:
        return [], ["CSV header must include at least a 'username' column."]

    results: list[EnrollmentResult] = []
    errors: list[str] = []

    for row_number, raw_row in enumerate(reader, start=2):
        row = {
            (k.strip().lower() if k else ""): (v.strip() if isinstance(v, str) else "")
            for k, v in raw_row.items()
        }
        if not any(row.values()):
            continue
        username = row.get("username", "")
        try:
            result = enroll_user(
                username=username,
                email=row.get("email", ""),
                first_name=row.get("first_name", ""),
                last_name=row.get("last_name", ""),
                role=row.get("role", "") or User.ROLE_EMPLOYEE,
                department=row.get("department", ""),
                send_invite=send_invite,
                reset_existing=reset_existing,
                request=request,
                login_url=login_url,
            )
            results.append(result)
        except ValidationError as exc:
            msg = "; ".join(exc.messages)
            errors.append(f"Row {row_number} ({username or 'missing username'}): {msg}")

    if not results and not errors:
        errors.append("No user rows were found in the CSV file.")

    return results, errors
