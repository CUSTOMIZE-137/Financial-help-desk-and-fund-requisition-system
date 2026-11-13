"""HTML views and authenticated REST endpoints for requisitions."""

import csv
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from app.workflow import ROLE_STAGE_MAP
from requisitions.cache import (
    get_cached_dashboard_counts,
    get_cached_requisition_pdf,
)

from requisitions.enrollment import (
    enroll_user,
    enroll_users_from_csv,
    issue_temporary_credentials,
)
from requisitions.entra_auth import (
    EntraAccessDeniedError,
    EntraAuthError,
    OIDC_SESSION_KEY,
    OIDC_STATE_MAX_AGE_SECONDS,
    authenticate_or_provision_entra_user,
    build_authorization_request,
    build_logout_url,
    exchange_code_for_tokens,
    is_microsoft_auth_enabled,
    verify_id_token,
)
from requisitions.forms import (
    AccountabilityForm,
    BulkUserEnrollmentForm,
    FirstLoginPasswordChangeForm,
    RequisitionForm,
    UserEnrollmentForm,
    UserSignatureForm,
)
from requisitions.models import (
    AccountabilitySubmission,
    Attachment,
    Requisition,
    RequisitionItem,
    RequisitionLog,
    User,
)
from requisitions.pdf import build_requisition_pdf
from requisitions.serializers import (
    AccountabilitySubmissionSerializer,
    AttachmentSerializer,
    LoginSerializer,
    PasswordChangeSerializer,
    RequisitionActionSerializer,
    RequisitionLogSerializer,
    RequisitionSerializer,
    UserSerializer,
)
from requisitions.workflow import (
    active_delegate_roles,
    can_access_approval_queue,
    can_download_requisition_form,
    can_execute_action,
    filter_queryset_by_role,
    transition_requisition,
)


def _login_throttle_key(request, username: str) -> str:
    """Build a cache key scoped to the source address and attempted username."""
    client_ip = request.META.get("REMOTE_ADDR", "unknown").strip() or "unknown"
    normalized_username = (username or "").strip().lower()[:150]
    return f"fh:login-fail:{client_ip}:{normalized_username}"


def _login_is_throttled(request, username: str) -> bool:
    """Return whether repeated failed attempts have reached the configured limit."""
    limit = max(int(getattr(settings, "LOGIN_RATE_LIMIT", 10)), 1)
    return int(cache.get(_login_throttle_key(request, username), 0) or 0) >= limit


def _record_failed_login(request, username: str) -> None:
    """Record a failed login attempt with a bounded cache lifetime."""
    key = _login_throttle_key(request, username)
    window = max(int(getattr(settings, "LOGIN_RATE_WINDOW_SECONDS", 300)), 1)
    if cache.add(key, 1, timeout=window):
        return
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)


def _clear_failed_logins(request, username: str) -> None:
    """Clear failed-attempt state after a successful login."""
    cache.delete(_login_throttle_key(request, username))


@require_GET
def healthz_view(request):
    """Lightweight liveness probe for container orchestrators and load balancers."""
    return JsonResponse({"status": "ok", "service": "financial-helpdesk"})


@require_GET
def readyz_view(request):
    """Readiness probe verifying database, cache, and file storage connectivity."""
    checks = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        healthy = False

    try:
        probe_key = "fh:readyz_probe"
        cache.set(probe_key, "1", timeout=10)
        if cache.get(probe_key) == "1":
            checks["cache"] = "ok"
        else:
            checks["cache"] = "error: cache read mismatch"
            healthy = False
    except Exception as exc:
        checks["cache"] = f"error: {exc}"
        healthy = False

    try:
        _ = default_storage.__class__.__name__
        checks["storage"] = "ok"
    except Exception as exc:
        checks["storage"] = f"error: {exc}"
        healthy = False

    status_code = 200 if healthy else 503
    return JsonResponse(
        {"status": "ready" if healthy else "degraded", "checks": checks},
        status=status_code,
    )


def _dashboard_context(user):
    """Build role-scoped dashboard totals (in a single cached aggregate query) and latest requests."""
    requisitions = filter_queryset_by_role(
        user, Requisition.objects.select_related("requestor")
    )

    def _compute_counts():
        agg = requisitions.aggregate(
            total=Count("id", distinct=True),
            in_review=Count(
                "id",
                filter=Q(
                    status__in=[
                        Requisition.STATUS_PENDING_SUPERVISOR,
                        Requisition.STATUS_PENDING_ME,
                        Requisition.STATUS_PENDING_PROGRAM_LEAD,
                        Requisition.STATUS_PENDING_EXECUTIVE_DIRECTOR,
                        Requisition.STATUS_PENDING_FINANCE,
                    ]
                ),
                distinct=True,
            ),
            accountability=Count(
                "id",
                filter=Q(
                    status__in=[
                        Requisition.STATUS_DISBURSED,
                        Requisition.STATUS_AWAITING_ACCOUNTABILITY,
                    ]
                ),
                distinct=True,
            ),
            completed=Count(
                "id",
                filter=Q(status=Requisition.STATUS_COMPLETED),
                distinct=True,
            ),
        )
        return {
            "total": agg["total"] or 0,
            "in_review": agg["in_review"] or 0,
            "accountability": agg["accountability"] or 0,
            "completed": agg["completed"] or 0,
        }

    return {
        "requisitions": list(requisitions[:10]),
        "can_download_form": can_download_requisition_form(user),
        "counts": get_cached_dashboard_counts(user, _compute_counts),
    }


@login_required
def dashboard_view(request):
    """Render the signed-in user's request summary."""
    return render(request, "requisitions/dashboard.html", _dashboard_context(request.user))


def _report_period(period, year, month=None, quarter=None):
    """Return the inclusive start and exclusive end dates for a report period."""
    if period == "month":
        start = date(year, month, 1)
        end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    elif period == "quarter":
        start_month = ((quarter - 1) * 3) + 1
        start = date(year, start_month, 1)
        end = date(year + (start_month == 10), 1 if start_month == 10 else start_month + 3, 1)
    else:
        start = date(year, 1, 1)
        end = date(year + 1, 1, 1)
    return start, end


@login_required
def internal_audit_report_view(request):
    """Generate a requisition report for Internal Audit by month, quarter, or year."""
    if request.user.role != User.ROLE_INTERNAL_AUDIT:
        raise PermissionDenied

    today = timezone.localdate()
    period = request.GET.get("period", "month")
    if period not in {"month", "quarter", "year"}:
        period = "month"
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        quarter = int(request.GET.get("quarter", ((today.month - 1) // 3) + 1))
        if not 2000 <= year <= 2100 or not 1 <= month <= 12 or not 1 <= quarter <= 4:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError("Report period values are invalid.")

    start, end = _report_period(period, year, month=month, quarter=quarter)
    requisitions = list(
        Requisition.objects.filter(requested_at__date__gte=start, requested_at__date__lt=end)
        .select_related("requestor")
        .prefetch_related("accountability_submissions")
        .order_by("requested_at", "pk")
    )
    rows = []
    totals = {}
    for requisition in requisitions:
        accountability = requisition.accountability_submissions.first()
        spent = accountability.actual_amount_spent if accountability else Decimal("0.00")
        totals[requisition.currency] = totals.get(requisition.currency, Decimal("0.00")) + requisition.amount
        rows.append(
            {
                "requisition": requisition,
                "spent": spent,
                "unspent": requisition.amount - spent,
            }
        )

    period_label = {
        "month": start.strftime("%B %Y"),
        "quarter": f"Q{quarter} {year}",
        "year": str(year),
    }[period]
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="internal-audit-{period_label.replace(" ", "-")}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Request", "Requestor", "Department", "Currency", "Approved amount", "Actual spent", "Unspent", "Status", "Requested"])
        for row in rows:
            requisition = row["requisition"]
            writer.writerow(
                [
                    requisition.title,
                    requisition.requestor.get_full_name() or requisition.requestor.username,
                    requisition.department,
                    requisition.currency,
                    requisition.amount,
                    row["spent"],
                    row["unspent"],
                    requisition.get_status_display(),
                    requisition.requested_at.strftime("%Y-%m-%d"),
                ]
            )
        return response

    return render(
        request,
        "requisitions/internal_audit_report.html",
        {
            "rows": rows,
            "totals": totals,
            "period": period,
            "period_label": period_label,
            "selected_year": year,
            "selected_month": month,
            "selected_quarter": quarter,
            "year_choices": range(today.year - 5, today.year + 1),
            "month_choices": range(1, 13),
            "quarter_choices": range(1, 5),
        },
    )


@login_required
def approval_queue_view(request):
    """Show requests on which the signed-in user can currently act."""
    if not can_access_approval_queue(request.user):
        raise PermissionDenied
    actionable_statuses = tuple(ROLE_STAGE_MAP.values())
    candidates = filter_queryset_by_role(
        request.user,
        Requisition.objects.filter(status__in=actionable_statuses).select_related(
            "requestor"
        ),
    )
    delegate_roles = active_delegate_roles(request.user)
    action_names = (
        "APPROVE",
        "REQUEST_REVISION",
        "REJECT",
        "FINALIZE_ACCOUNTABILITY",
    )
    queue = []
    can_download = can_download_requisition_form(request.user, delegate_roles=delegate_roles)
    for item in candidates:
        allowed_actions = [
            action
            for action in action_names
            if can_execute_action(request.user, item, action, delegate_roles=delegate_roles)
        ]
        if allowed_actions:
            queue.append({
                "requisition": item,
                "allowed_actions": allowed_actions,
                "can_download_form": can_download,
            })
    return render(request, "requisitions/approval_queue.html", {"requisitions": queue})


@require_http_methods(["GET", "POST"])
def login_view(request):
    """Authenticate browser credentials and allow only safe local redirects."""
    if request.user.is_authenticated:
        if getattr(request.user, "must_change_password", False):
            return redirect("password-change")
        return redirect("dashboard")
    microsoft_enabled = is_microsoft_auth_enabled()
    allow_local_login = bool(getattr(settings, "MICROSOFT_ALLOW_LOCAL_LOGIN", True))
    next_url = request.GET.get("next", "")
    if request.method == "POST":
        if microsoft_enabled and not allow_local_login:
            messages.error(
                request,
                "Local password sign-in is disabled. Please sign in with Microsoft.",
            )
            return render(
                request,
                "requisitions/login.html",
                {
                    "microsoft_auth_enabled": microsoft_enabled,
                    "allow_local_login": allow_local_login,
                    "next_url": next_url,
                },
                status=403,
            )
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        if _login_is_throttled(request, username):
            messages.error(request, "Too many failed sign-in attempts. Please try again later.")
            return render(
                request,
                "requisitions/login.html",
                {
                    "microsoft_auth_enabled": microsoft_enabled,
                    "allow_local_login": allow_local_login,
                    "next_url": next_url,
                },
                status=429,
            )
        user = authenticate(request, username=username, password=password)
        if user is not None:
            _clear_failed_logins(request, username)
            login(request, user)
            if getattr(user, "must_change_password", False):
                messages.info(
                    request,
                    "Please set a new personal password to complete your account enrollment.",
                )
                return redirect("password-change")
            if url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect("dashboard")
        _record_failed_login(request, username)
        messages.error(request, "Invalid username or password.")
    return render(
        request,
        "requisitions/login.html",
        {
            "microsoft_auth_enabled": microsoft_enabled,
            "allow_local_login": allow_local_login,
            "next_url": next_url,
        },
    )


@require_POST
def logout_view(request):
    """End the browser session using a CSRF-protected POST."""
    was_microsoft_user = bool(
        request.user.is_authenticated and getattr(request.user, "entra_object_id", "")
    )
    logout(request)
    if (
        was_microsoft_user
        and request.POST.get("microsoft_logout") == "1"
        and is_microsoft_auth_enabled()
    ):
        try:
            return redirect(build_logout_url(request))
        except EntraAuthError:
            pass
    return redirect("login")


@require_GET
def microsoft_login_view(request):
    """Start the Microsoft Entra ID OIDC + PKCE authorization code flow."""
    if not is_microsoft_auth_enabled():
        messages.error(request, "Microsoft sign-in is not currently enabled.")
        return redirect("login")

    raw_next = request.GET.get("next", "")
    safe_next = (
        raw_next
        if url_has_allowed_host_and_scheme(
            raw_next,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        else ""
    )
    teams_popup = request.GET.get("teams_popup") == "1"
    login_hint = request.GET.get("login_hint", "").strip()
    try:
        authorize_url = build_authorization_request(
            request,
            next_url=safe_next,
            teams_popup=teams_popup,
            login_hint=login_hint,
        )
    except EntraAuthError as exc:
        messages.error(request, exc.user_message)
        return redirect("login")
    return redirect(authorize_url)


@require_GET
def microsoft_callback_view(request):
    """Validate the Entra ID OIDC callback, link/provision the user, and start a session."""
    import secrets
    import time

    if not is_microsoft_auth_enabled():
        messages.error(request, "Microsoft sign-in is not currently enabled.")
        return redirect("login")

    flow = request.session.pop(OIDC_SESSION_KEY, None)
    is_teams_popup = bool(isinstance(flow, dict) and flow.get("teams_popup"))

    def _fail(user_msg: str, status_code: int = 400):
        if is_teams_popup:
            return render(
                request,
                "requisitions/teams_auth_end.html",
                {"auth_success": False, "error_message": user_msg},
                status=status_code,
            )
        messages.error(request, user_msg)
        return redirect("login")

    oauth_error = request.GET.get("error", "").strip()
    if oauth_error:
        return _fail("Microsoft sign-in was cancelled or denied by the identity provider.")

    if not isinstance(flow, dict):
        return _fail("Your sign-in session expired or is invalid. Please try again.")

    created_at = int(flow.get("created_at") or 0)
    if (int(time.time()) - created_at) > OIDC_STATE_MAX_AGE_SECONDS:
        return _fail("Your sign-in request timed out. Please try signing in again.")

    returned_state = request.GET.get("state", "")
    expected_state = str(flow.get("state") or "")
    if (
        not returned_state
        or not expected_state
        or not secrets.compare_digest(returned_state, expected_state)
    ):
        return _fail("Invalid authentication state. Please try signing in again.")

    code = request.GET.get("code", "").strip()
    if not code:
        return _fail("Authorization code was not provided by Microsoft.")

    try:
        token_data = exchange_code_for_tokens(
            code=code,
            code_verifier=str(flow.get("code_verifier") or ""),
            redirect_uri=str(flow.get("redirect_uri") or ""),
        )
        id_token = str(token_data.get("id_token") or "")
        access_token = str(token_data.get("access_token") or "")
        claims = verify_id_token(
            id_token,
            expected_nonce=str(flow.get("nonce") or ""),
        )
        sync_result = authenticate_or_provision_entra_user(
            claims,
            access_token=access_token,
        )
    except EntraAccessDeniedError as exc:
        if is_teams_popup:
            return render(
                request,
                "requisitions/teams_auth_end.html",
                {"auth_success": False, "error_message": exc.user_message},
                status=403,
            )
        messages.error(request, exc.user_message)
        return render(
            request,
            "requisitions/login.html",
            {
                "microsoft_auth_enabled": True,
                "allow_local_login": bool(
                    getattr(settings, "MICROSOFT_ALLOW_LOCAL_LOGIN", True)
                ),
                "next_url": "",
            },
            status=403,
        )
    except EntraAuthError as exc:
        return _fail(exc.user_message)

    login(
        request,
        sync_result.user,
        backend="django.contrib.auth.backends.ModelBackend",
    )
    if is_teams_popup:
        return redirect("teams-auth-end")

    next_url = str(flow.get("next_url") or "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("dashboard")


@require_GET
def teams_tab_view(request):
    """Entry point for Microsoft Teams personal and channel tabs."""
    target = request.GET.get("target", "dashboard").strip().lower()
    if target == "approval-queue":
        destination_url = reverse("approval-queue")
    else:
        destination_url = reverse("dashboard")

    if request.user.is_authenticated:
        if target == "approval-queue" and can_access_approval_queue(request.user):
            return redirect("approval-queue")
        return redirect("dashboard")

    return render(
        request,
        "requisitions/teams_tab.html",
        {
            "destination_url": destination_url,
            "microsoft_auth_enabled": is_microsoft_auth_enabled(),
        },
    )


@require_GET
def teams_auth_start_view(request):
    """Launch Entra ID OIDC login inside a Microsoft Teams authentication popup."""
    if not is_microsoft_auth_enabled():
        return render(
            request,
            "requisitions/teams_auth_end.html",
            {
                "auth_success": False,
                "error_message": "Microsoft Entra ID SSO is not enabled.",
            },
            status=400,
        )
    login_hint = request.GET.get("loginHint", "").strip()
    try:
        authorize_url = build_authorization_request(
            request,
            teams_popup=True,
            login_hint=login_hint,
        )
    except EntraAuthError as exc:
        return render(
            request,
            "requisitions/teams_auth_end.html",
            {"auth_success": False, "error_message": exc.user_message},
            status=400,
        )
    return redirect(authorize_url)


@require_GET
def teams_auth_end_view(request):
    """Complete the Microsoft Teams popup authentication handshake."""
    return render(
        request,
        "requisitions/teams_auth_end.html",
        {
            "auth_success": bool(request.user.is_authenticated),
            "error_message": ""
            if request.user.is_authenticated
            else "Authentication did not complete.",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def password_change_view(request):
    """Require users with temporary credentials (or any signed-in user) to set a new password."""
    is_first_login = bool(getattr(request.user, "must_change_password", False))
    form = FirstLoginPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(
            request,
            "Your password has been updated. Welcome to FinanceDesk!",
        )
        return redirect("dashboard")
    return render(
        request,
        "requisitions/password_change.html",
        {"form": form, "is_first_login": is_first_login},
    )


@login_required
@require_http_methods(["GET", "POST"])
def user_enrollment_view(request):
    """Allow administrators to enroll users, bulk-import CSV rosters, and re-issue invitations."""
    if not (request.user.is_superuser or request.user.role == User.ROLE_ADMIN):
        raise PermissionDenied

    if request.GET.get("format") == "csv-template":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="staff-enrollment-template.csv"'
        writer = csv.writer(response)
        writer.writerow(["username", "email", "first_name", "last_name", "role", "department"])
        writer.writerow(["jdoe", "jdoe@example.org", "Jane", "Doe", "EMPLOYEE", "Programs"])
        writer.writerow(["msmith", "msmith@example.org", "Mark", "Smith", "SUPERVISOR", "Programs"])
        return response

    single_form = UserEnrollmentForm()
    bulk_form = BulkUserEnrollmentForm()
    enrolled_results = []
    bulk_errors = []

    if request.method == "POST":
        action = request.POST.get("enrollment_action", "single")
        if action == "single":
            single_form = UserEnrollmentForm(request.POST)
            if single_form.is_valid():
                try:
                    result = enroll_user(
                        username=single_form.cleaned_data["username"],
                        email=single_form.cleaned_data["email"],
                        first_name=single_form.cleaned_data["first_name"],
                        last_name=single_form.cleaned_data["last_name"],
                        role=single_form.cleaned_data["role"],
                        department=single_form.cleaned_data["department"],
                        send_invite=single_form.cleaned_data["send_invitation"],
                        request=request,
                    )
                except ValidationError as exc:
                    for error in exc.messages:
                        single_form.add_error(None, error)
                else:
                    enrolled_results = [result]
                    single_form = UserEnrollmentForm()
                    if result.email_sent:
                        messages.success(
                            request,
                            f"Enrolled '{result.user.username}' and sent an invitation to {result.user.email}.",
                        )
                    elif result.email_error:
                        messages.warning(
                            request,
                            f"Enrolled '{result.user.username}', but the invitation email could not be sent: {result.email_error}",
                        )
                    else:
                        messages.success(
                            request,
                            f"Enrolled '{result.user.username}' with temporary credentials.",
                        )
        elif action == "bulk_csv":
            bulk_form = BulkUserEnrollmentForm(request.POST, request.FILES)
            if bulk_form.is_valid():
                uploaded_csv = bulk_form.cleaned_data["csv_file"]
                enrolled_results, bulk_errors = enroll_users_from_csv(
                    uploaded_csv.read(),
                    send_invite=bulk_form.cleaned_data["send_invitation"],
                    reset_existing=bulk_form.cleaned_data["reset_existing"],
                    request=request,
                )
                if enrolled_results:
                    messages.success(
                        request,
                        f"Successfully enrolled {len(enrolled_results)} user account(s) from CSV.",
                    )
                    bulk_form = BulkUserEnrollmentForm()
                for err in bulk_errors:
                    messages.error(request, err)
        elif action == "reinvite":
            target_user = get_object_or_404(User, pk=request.POST.get("user_id"))
            result = issue_temporary_credentials(
                target_user,
                send_invite=True,
                request=request,
            )
            enrolled_results = [result]
            if result.email_sent:
                messages.success(
                    request,
                    f"Issued a new temporary password for '{target_user.username}' and sent an invitation to {target_user.email}.",
                )
            elif result.email_error:
                messages.warning(
                    request,
                    f"Issued a new temporary password for '{target_user.username}', but email delivery failed: {result.email_error}",
                )
            else:
                messages.success(
                    request,
                    f"Issued a new temporary password for '{target_user.username}'.",
                )

    users = User.objects.all().order_by("username")
    return render(
        request,
        "requisitions/user_enrollment.html",
        {
            "single_form": single_form,
            "bulk_form": bulk_form,
            "enrolled_results": enrolled_results,
            "users": users,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def signature_settings_view(request):
    """Allow the signed-in user to upload or update their saved signature image."""
    form = UserSignatureForm(
        request.POST or None, request.FILES or None, instance=request.user
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your e-signature image has been saved.")
        return redirect("signature-settings")
    return render(request, "requisitions/signature_form.html", {"form": form})


def _save_requisition_items(requisition, post_data):
    """Save validated budget rows using a single bulk_create and recalculate the total."""
    descriptions = post_data.getlist("item_description[]")
    quantities = post_data.getlist("quantity[]")
    unit_costs = post_data.getlist("unit_cost[]")
    requisition.items.all().delete()
    total_sum = Decimal("0.00")
    items_to_create = []
    for desc, qty, unit_cost in zip(descriptions, quantities, unit_costs):
        desc = desc.strip()
        if not desc:
            continue
        qty_dec = Decimal(qty.strip())
        unit_cost_dec = Decimal(unit_cost.strip())
        total_dec = (qty_dec * unit_cost_dec).quantize(Decimal("0.01"))
        items_to_create.append(
            RequisitionItem(
                requisition=requisition,
                item_description=desc,
                quantity=qty_dec,
                unit_cost=unit_cost_dec,
                total_cost=total_dec,
            )
        )
        total_sum += total_dec
    if items_to_create:
        RequisitionItem.objects.bulk_create(items_to_create)
    requisition.amount = total_sum
    requisition.save(update_fields=["amount"])


def _budget_rows_for_form(request, requisition=None):
    """Build budget-table rows from submitted data or an existing request."""
    if request.method == "POST":
        descriptions = request.POST.getlist("item_description[]")
        quantities = request.POST.getlist("quantity[]")
        unit_costs = request.POST.getlist("unit_cost[]")
        total_costs = request.POST.getlist("total_cost[]")
        row_count = max(len(descriptions), len(quantities), len(unit_costs), 1)
        return [
            {
                "description": descriptions[index] if index < len(descriptions) else "",
                "quantity": quantities[index] if index < len(quantities) else "",
                "unit_cost": unit_costs[index] if index < len(unit_costs) else "",
                "total_cost": total_costs[index] if index < len(total_costs) else "",
            }
            for index in range(row_count)
        ]
    if requisition:
        return [
            {
                "description": item.item_description,
                "quantity": item.quantity,
                "unit_cost": item.unit_cost,
                "total_cost": item.total_cost,
            }
            for item in requisition.items.all()
        ] or [{"description": "", "quantity": "1", "unit_cost": "", "total_cost": ""}]
    return [{"description": "", "quantity": "1", "unit_cost": "", "total_cost": ""}]


@login_required
def requisition_create_view(request):
    """Create a request owned by the signed-in employee."""
    if request.user.role != User.ROLE_EMPLOYEE and not (
        request.user.is_superuser or request.user.role == User.ROLE_ADMIN
    ):
        raise PermissionDenied
    form = RequisitionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        requisition = form.save(commit=False)
        requisition.requestor = request.user
        requisition.department = request.user.department
        requisition.save()
        _save_requisition_items(requisition, request.POST)

        action_type = request.POST.get("action_type")
        if action_type == "submit":
            try:
                transition_requisition(requisition.pk, request.user, "SUBMIT")
                messages.success(request, "Requisition submitted successfully.")
            except Exception as exc:
                messages.warning(request, f"Requisition saved as draft: {exc}")
        else:
            messages.success(request, "Draft requisition created.")
        return redirect("requisition-detail", pk=requisition.pk)
    return render(
        request,
        "requisitions/requisition_form.html",
        {
            "form": form,
            "is_edit": False,
            "budget_rows": _budget_rows_for_form(request),
        },
    )


@login_required
def requisition_edit_view(request, pk):
    """Let an owner or administrator edit a draft or returned request."""
    requisition = get_object_or_404(
        filter_queryset_by_role(request.user, Requisition.objects.all()), pk=pk
    )
    can_edit = (
        requisition.requestor_id == request.user.pk
        and requisition.status in (Requisition.STATUS_DRAFT, Requisition.STATUS_REVISION_REQUESTED)
    ) or (
        (request.user.is_superuser or request.user.role == User.ROLE_ADMIN)
        and requisition.status in (Requisition.STATUS_DRAFT, Requisition.STATUS_REVISION_REQUESTED)
    )
    if not can_edit:
        raise PermissionDenied
    form = RequisitionForm(request.POST or None, instance=requisition)
    if request.method == "POST" and form.is_valid():
        requisition = form.save()
        _save_requisition_items(requisition, request.POST)

        action_type = request.POST.get("action_type")
        if action_type == "submit":
            action = "RESUBMIT" if requisition.status == Requisition.STATUS_REVISION_REQUESTED else "SUBMIT"
            try:
                transition_requisition(requisition.pk, request.user, action)
                messages.success(request, "Requisition submitted successfully.")
            except Exception as exc:
                messages.warning(request, f"Requisition updated, but submit failed: {exc}")
        else:
            messages.success(request, "Requisition updated.")
        return redirect("requisition-detail", pk=requisition.pk)
    return render(
        request,
        "requisitions/requisition_form.html",
        {
            "form": form,
            "is_edit": True,
            "requisition": requisition,
            "budget_rows": _budget_rows_for_form(request, requisition),
        },
    )


@login_required
def requisition_detail_view(request, pk):
    """Render an authorized request with its history and available actions."""
    requisition = get_object_or_404(
        filter_queryset_by_role(
            request.user,
            Requisition.objects.select_related("requestor").prefetch_related(
                "items", "logs__actor", "accountability_submissions"
            ),
        ),
        pk=pk,
    )
    delegate_roles = active_delegate_roles(request.user)
    actions = (
        "SUBMIT",
        "APPROVE",
        "REQUEST_REVISION",
        "REJECT",
        "RESUBMIT",
        "SUBMIT_ACCOUNTABILITY",
        "FINALIZE_ACCOUNTABILITY",
    )
    allowed_actions = [
        action
        for action in actions
        if can_execute_action(request.user, requisition, action, delegate_roles=delegate_roles)
    ]
    return render(
        request,
        "requisitions/requisition_detail.html",
        {
            "requisition": requisition,
            "items": requisition.items.all(),
            "logs": requisition.logs.select_related("actor"),
            "accountability": requisition.accountability_submissions.first(),
            "allowed_actions": allowed_actions,
            "can_download_form": can_download_requisition_form(
                request.user, requisition, delegate_roles=delegate_roles
            ),
            "can_edit": (
                requisition.requestor_id == request.user.pk
                or request.user.is_superuser
                or request.user.role == User.ROLE_ADMIN
            )
            and requisition.status
            in (Requisition.STATUS_DRAFT, Requisition.STATUS_REVISION_REQUESTED),
        },
    )


@login_required
@require_POST
def requisition_action_view(request, pk):
    """Apply a validated workflow action and report any expected rejection."""
    requisition = get_object_or_404(
        filter_queryset_by_role(request.user, Requisition.objects.all()), pk=pk
    )
    action = request.POST.get("action", "")
    comments = request.POST.get("comments", "")
    try:
        transition_requisition(
            requisition.pk,
            request.user,
            action,
            comments,
            request.POST.get("disbursement_method", ""),
            request.POST.get("budget_line", ""),
        )
    except (PermissionDenied, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            errors = exc.messages
        else:
            errors = [str(exc)]
        for error in errors:
            messages.error(request, error)
    else:
        messages.success(request, "Requisition status updated.")
        if action in (
            "APPROVE",
            "REQUEST_REVISION",
            "REJECT",
            "FINALIZE_ACCOUNTABILITY",
        ):
            return redirect("approval-queue")
    return redirect("requisition-detail", pk=pk)


@login_required
def accountability_create_view(request, pk):
    """Save the requester's liquidation evidence and advance it for review."""
    requisition = get_object_or_404(
        filter_queryset_by_role(request.user, Requisition.objects.all()), pk=pk
    )
    if not can_execute_action(request.user, requisition, "SUBMIT_ACCOUNTABILITY"):
        raise PermissionDenied
    form = AccountabilityForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                submission = form.save(commit=False)
                submission.requisition = requisition
                submission.submitted_by = request.user
                submission.full_clean()
                submission.save()
                transition_requisition(
                    requisition.pk,
                    request.user,
                    "SUBMIT_ACCOUNTABILITY",
                    "Accountability evidence submitted.",
                )
        except PermissionDenied as exc:
            messages.error(request, str(exc))
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
        else:
            messages.success(request, "Accountability submitted for Internal Audit review.")
            return redirect("requisition-detail", pk=pk)
    return render(
        request,
        "requisitions/accountability_form.html",
        {"form": form, "requisition": requisition},
    )


@login_required
def protected_file_view(request, kind, pk):
    """Serve a file only after checking the signed-in user's request access."""
    return _protected_file_response(request.user, kind, pk)


@login_required
def requisition_download_view(request, pk):
    """Download the printable PDF requisition form for Finance filing."""
    return _requisition_pdf_response(request.user, pk)


def _requisition_pdf_response(user, pk):
    """Generate and return a PDF requisition form for an authorized finance user."""
    requisition = get_object_or_404(
        filter_queryset_by_role(
            user,
            Requisition.objects.select_related("requestor").prefetch_related(
                "items", "logs__actor"
            ),
        ),
        pk=pk,
    )
    if not can_download_requisition_form(user, requisition):
        raise PermissionDenied("Only Finance officers and administrators can download requisition forms.")
    pdf_bytes = get_cached_requisition_pdf(
        requisition, lambda: build_requisition_pdf(requisition)
    )
    filename = get_valid_filename(f"requisition-{requisition.pk}.pdf")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _protected_file_response(user, kind, pk):
    """Resolve and stream (or presign-redirect) a protected evidence file for an authorized user."""
    if kind == "signature":
        if user.pk != pk and not (user.is_superuser or user.role == User.ROLE_ADMIN):
            raise Http404
        target_user = get_object_or_404(User, pk=pk)
        field = target_user.signature_image
        if not field:
            raise Http404
        if getattr(settings, "STORAGE_USE_SIGNED_REDIRECT", False):
            return redirect(field.storage.url(field.name))
        try:
            file_handle = field.open("rb")
        except OSError as exc:
            raise Http404("The requested file is unavailable.") from exc
        response = FileResponse(
            file_handle,
            as_attachment=False,
            filename=get_valid_filename(field.name.rsplit("/", 1)[-1]),
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    if kind == "attachment":
        record = get_object_or_404(Attachment.objects.select_related("requisition"), pk=pk)
        field = record.file
        requisition = record.requisition
    elif kind in ("receipt", "invoice"):
        record = get_object_or_404(
            AccountabilitySubmission.objects.select_related("requisition"), pk=pk
        )
        field = getattr(record, f"{kind}_file")
        requisition = record.requisition
    else:
        raise Http404
    if not field or not filter_queryset_by_role(
        user, Requisition.objects.filter(pk=requisition.pk)
    ).exists():
        raise Http404
    if getattr(settings, "STORAGE_USE_SIGNED_REDIRECT", False):
        return redirect(field.storage.url(field.name))
    try:
        file_handle = field.open("rb")
    except OSError as exc:
        raise Http404("The requested file is unavailable.") from exc
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=get_valid_filename(field.name.rsplit("/", 1)[-1]),
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class IsSystemAdmin(permissions.BasePermission):
    """Restrict user-account administration to system administrators."""

    def has_permission(self, request, view):
        """Allow only superusers or users assigned the administrator role."""
        return bool(
            request.user
            and request.user.is_authenticated
            and not getattr(request.user, "must_change_password", False)
            and (request.user.is_superuser or request.user.role == User.ROLE_ADMIN)
        )


class UserListCreateView(generics.ListCreateAPIView):
    """List users or create accounts through the administrator-only API."""

    serializer_class = UserSerializer
    permission_classes = [IsSystemAdmin]
    queryset = User.objects.all()


class RequisitionListCreateView(generics.ListCreateAPIView):
    """List requests visible to the user and create employee-owned drafts."""

    serializer_class = RequisitionSerializer

    def get_queryset(self):
        """Apply role-based visibility and preload related display fields."""
        return filter_queryset_by_role(
            self.request.user,
            Requisition.objects.select_related("requestor").prefetch_related("items"),
        )

    def perform_create(self, serializer):
        """Enforce creator roles and set ownership and department server-side."""
        user = self.request.user
        if user.role != User.ROLE_EMPLOYEE and not (
            user.is_superuser or user.role == User.ROLE_ADMIN
        ):
            raise DRFPermissionDenied("Only employees and administrators can create requisitions.")
        serializer.save(
            requestor=user,
            department=user.department,
            status=Requisition.STATUS_DRAFT,
        )


class RequisitionDetailView(generics.RetrieveUpdateAPIView):
    """Retrieve requests and allow owners or administrators to edit drafts or returned items."""

    serializer_class = RequisitionSerializer

    def get_queryset(self):
        """Limit detail and update lookups to requests visible to the user."""
        return filter_queryset_by_role(
            self.request.user, Requisition.objects.prefetch_related("items")
        )

    def _check_can_edit(self, user, requisition):
        """Verify owner or administrator role and editable request status."""
        is_owner_or_admin = (
            requisition.requestor_id == user.pk
            or user.is_superuser
            or user.role == User.ROLE_ADMIN
        )
        if not is_owner_or_admin or requisition.status not in (
            Requisition.STATUS_DRAFT,
            Requisition.STATUS_REVISION_REQUESTED,
        ):
            raise DRFPermissionDenied(
                "Only the requester or an administrator can edit a draft or returned requisition."
            )

    def update(self, request, *args, **kwargs):
        """Authorize full updates only for the owner or admin of an editable request."""
        requisition = self.get_object()
        self._check_can_edit(request.user, requisition)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        """Apply the same owner/admin and state restrictions to partial updates."""
        requisition = self.get_object()
        self._check_can_edit(request.user, requisition)
        return super().partial_update(request, *args, **kwargs)


class RequisitionActionView(APIView):
    """Apply role-authorized workflow actions through the REST API."""

    def post(self, request, pk):
        """Validate an action, execute the transaction, and return the new request."""
        requisition = get_object_or_404(
            filter_queryset_by_role(request.user, Requisition.objects.all()), pk=pk
        )
        serializer = RequisitionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = transition_requisition(
                requisition.pk,
                request.user,
                serializer.validated_data["action"],
                serializer.validated_data.get("comments", ""),
                serializer.validated_data.get("disbursement_method", ""),
                serializer.validated_data.get("budget_line", ""),
            )
        except PermissionDenied as exc:
            raise DRFPermissionDenied(str(exc)) from exc
        except ValidationError as exc:
            raise DRFValidationError(exc.messages) from exc
        return Response(RequisitionSerializer(updated).data)


class LoginAPIView(APIView):
    """Create a Django session from valid API credentials."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """Authenticate the supplied credentials and start a browser-compatible session."""
        username = str(request.data.get("username", "")).strip()
        if _login_is_throttled(request, username):
            return Response(
                {"detail": "Too many failed sign-in attempts. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        serializer = LoginSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            _record_failed_login(request, username)
            raise DRFValidationError(serializer.errors)
        user = serializer.validated_data["user"]
        _clear_failed_logins(request, username)
        login(request, user)
        return Response(
            {
                "detail": "Authenticated",
                "user_id": user.pk,
                "must_change_password": bool(getattr(user, "must_change_password", False)),
            }
        )


class LogoutAPIView(APIView):
    """End the current authenticated API session."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        """Invalidate the session and return a confirmation."""
        logout(request)
        return Response({"detail": "Logged out successfully."})


class PasswordChangeAPIView(APIView):
    """Allow an authenticated API user to complete their first-login password reset."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        """Validate the current and new password, clear must_change_password, and keep session active."""
        serializer = PasswordChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        update_session_auth_hash(request, user)
        return Response(
            {"detail": "Password updated successfully.", "must_change_password": False}
        )


class ProtectedFileAPIView(APIView):
    """Serve evidence through the same access checks as browser downloads."""

    def get(self, request, kind, pk):
        """Return a protected file after verifying request visibility."""
        return _protected_file_response(request.user, kind, pk)


class RequisitionDownloadAPIView(APIView):
    """Download a printable PDF requisition form through the REST API."""

    def get(self, request, pk):
        """Return the generated PDF form if the user is authorized for Finance filing."""
        try:
            return _requisition_pdf_response(request.user, pk)
        except PermissionDenied as exc:
            raise DRFPermissionDenied(str(exc)) from exc


class RequisitionLogListView(generics.ListAPIView):
    """List audit history only for requests visible to the current user."""

    serializer_class = RequisitionLogSerializer

    def get_queryset(self):
        """Filter log entries by the user's visible request IDs."""
        visible_ids = filter_queryset_by_role(
            self.request.user, Requisition.objects.all()
        ).values("id")
        return RequisitionLog.objects.filter(requisition_id__in=visible_ids).select_related(
            "actor", "requisition"
        )


class AccountabilitySubmissionListCreateView(generics.ListCreateAPIView):
    """List visible liquidation reports and submit new reports for review."""

    serializer_class = AccountabilitySubmissionSerializer

    def get_queryset(self):
        """Return only submissions attached to requests visible to the user."""
        visible_ids = filter_queryset_by_role(
            self.request.user, Requisition.objects.all()
        ).values("id")
        return AccountabilitySubmission.objects.filter(
            requisition_id__in=visible_ids
        ).select_related("requisition", "submitted_by")

    def create(self, request, *args, **kwargs):
        """Save evidence and transition its request atomically."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requisition = serializer.validated_data["requisition"]
        if not can_execute_action(request.user, requisition, "SUBMIT_ACCOUNTABILITY"):
            raise DRFPermissionDenied(
                "Only the requester or finance staff can submit accountability evidence."
            )
        with transaction.atomic():
            submission = serializer.save(submitted_by=request.user)
            try:
                transition_requisition(
                    requisition.pk,
                    request.user,
                    "SUBMIT_ACCOUNTABILITY",
                    "Accountability evidence submitted.",
                )
            except (PermissionDenied, ValidationError) as exc:
                raise DRFValidationError(str(exc)) from exc
        return Response(self.get_serializer(submission).data, status=status.HTTP_201_CREATED)


class AttachmentListCreateView(generics.ListCreateAPIView):
    """List visible attachments and accept authorized evidence uploads."""

    serializer_class = AttachmentSerializer

    def get_queryset(self):
        """Return attachment metadata only for visible requests."""
        visible_ids = filter_queryset_by_role(
            self.request.user, Requisition.objects.all()
        ).values("id")
        return Attachment.objects.filter(requisition_id__in=visible_ids).select_related(
            "requisition", "uploaded_by"
        )

    def perform_create(self, serializer):
        """Enforce request visibility, uploader role, and open-request status."""
        requisition = serializer.validated_data["requisition"]
        if not filter_queryset_by_role(
            self.request.user, Requisition.objects.filter(pk=requisition.pk)
        ).exists():
            raise DRFPermissionDenied("You cannot upload files for this requisition.")
        if requisition.requestor_id != self.request.user.pk and self.request.user.role not in (
            User.ROLE_FINANCE,
            User.ROLE_ADMIN,
        ):
            raise DRFPermissionDenied("Only the requester or finance staff can upload files.")
        if requisition.status in (
            Requisition.STATUS_COMPLETED,
            Requisition.STATUS_REJECTED,
        ):
            raise DRFPermissionDenied("Files cannot be added to a closed requisition.")
        serializer.save(uploaded_by=self.request.user)
