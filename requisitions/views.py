"""HTML views and authenticated REST endpoints for requisitions."""

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from requisitions.forms import AccountabilityForm, RequisitionForm
from requisitions.models import (
    AccountabilitySubmission,
    Attachment,
    BudgetLine,
    Project,
    Requisition,
    RequisitionLog,
    User,
)
from requisitions.serializers import (
    AccountabilitySubmissionSerializer,
    AttachmentSerializer,
    BudgetLineSerializer,
    LoginSerializer,
    ProjectSerializer,
    RequisitionActionSerializer,
    RequisitionLogSerializer,
    RequisitionSerializer,
    UserSerializer,
)
from requisitions.workflow import (
    can_access_approval_queue,
    can_execute_action,
    filter_queryset_by_role,
    transition_requisition,
)


def _dashboard_context(user):
    """Build role-scoped dashboard totals and the latest visible requests."""
    requisitions = filter_queryset_by_role(user, Requisition.objects.select_related("project"))
    return {
        "requisitions": requisitions[:10],
        "counts": {
            "total": requisitions.count(),
            "in_review": requisitions.filter(
                status__in=[
                    Requisition.STATUS_PENDING_SUPERVISOR,
                    Requisition.STATUS_PENDING_ME,
                    Requisition.STATUS_PENDING_DIRECTOR,
                    Requisition.STATUS_PENDING_FINANCE,
                ]
            ).count(),
            "accountability": requisitions.filter(
                status__in=[
                    Requisition.STATUS_DISBURSED,
                    Requisition.STATUS_AWAITING_ACCOUNTABILITY,
                ]
            ).count(),
            "completed": requisitions.filter(status=Requisition.STATUS_COMPLETED).count(),
        },
    }


@login_required
def dashboard_view(request):
    """Render the signed-in user's request summary."""
    return render(request, "requisitions/dashboard.html", _dashboard_context(request.user))


@login_required
def approval_queue_view(request):
    """Show requests on which the signed-in user can currently act."""
    if not can_access_approval_queue(request.user):
        raise PermissionDenied
    candidates = filter_queryset_by_role(
        request.user, Requisition.objects.select_related("project", "requestor")
    )
    action_names = (
        "APPROVE",
        "REQUEST_REVISION",
        "REJECT",
        "FINALIZE_ACCOUNTABILITY",
    )
    queue = []
    for item in candidates:
        allowed_actions = [
            action
            for action in action_names
            if can_execute_action(request.user, item, action)
        ]
        if allowed_actions:
            queue.append({"requisition": item, "allowed_actions": allowed_actions})
    return render(request, "requisitions/approval_queue.html", {"requisitions": queue})


@require_http_methods(["GET", "POST"])
def login_view(request):
    """Authenticate browser credentials and allow only safe local redirects."""
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get("next", "")
            if url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect("dashboard")
        messages.error(request, "Invalid username or password.")
    return render(request, "requisitions/login.html")


@require_POST
def logout_view(request):
    """End the browser session using a CSRF-protected POST."""
    logout(request)
    return redirect("login")


@login_required
def requisition_create_view(request):
    """Create a draft request owned by the signed-in employee."""
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
        messages.success(request, "Draft requisition created.")
        return redirect("requisition-detail", pk=requisition.pk)
    return render(
        request,
        "requisitions/requisition_form.html",
        {"form": form, "is_edit": False},
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
        form.save()
        messages.success(request, "Requisition updated.")
        return redirect("requisition-detail", pk=requisition.pk)
    return render(
        request,
        "requisitions/requisition_form.html",
        {"form": form, "is_edit": True, "requisition": requisition},
    )


@login_required
def requisition_detail_view(request, pk):
    """Render an authorized request with its history and available actions."""
    requisition = get_object_or_404(
        filter_queryset_by_role(
            request.user,
            Requisition.objects.select_related("project", "budget_line", "requestor"),
        ),
        pk=pk,
    )
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
        action for action in actions if can_execute_action(request.user, requisition, action)
    ]
    return render(
        request,
        "requisitions/requisition_detail.html",
        {
            "requisition": requisition,
            "logs": requisition.logs.select_related("actor"),
            "accountability": requisition.accountability_submissions.first(),
            "allowed_actions": allowed_actions,
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
        transition_requisition(requisition.pk, request.user, action, comments)
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
            messages.success(request, "Accountability submitted for finance review.")
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


def _protected_file_response(user, kind, pk):
    """Resolve and stream a protected evidence file for an authorized user."""
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


class IsAdminOrReadOnly(permissions.BasePermission):
    """Allow authenticated reads and reserve project/budget writes for finance."""

    def has_permission(self, request, view):
        """Check the user's role before the API view handles the request."""
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return bool(
            request.user
            and request.user.is_authenticated
            and (
                request.user.is_superuser
                or request.user.role in (User.ROLE_ADMIN, User.ROLE_FINANCE)
            )
        )


class IsSystemAdmin(permissions.BasePermission):
    """Restrict user-account administration to system administrators."""

    def has_permission(self, request, view):
        """Allow only superusers or users assigned the administrator role."""
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role == User.ROLE_ADMIN)
        )


class UserListCreateView(generics.ListCreateAPIView):
    """List users or create accounts through the administrator-only API."""

    serializer_class = UserSerializer
    permission_classes = [IsSystemAdmin]
    queryset = User.objects.all()


class ProjectListCreateView(generics.ListCreateAPIView):
    """List active project data or let finance/admin staff manage projects."""

    serializer_class = ProjectSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = Project.objects.all()


class BudgetLineListCreateView(generics.ListCreateAPIView):
    """List budget allocations or allow finance/admin staff to maintain them."""

    serializer_class = BudgetLineSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = BudgetLine.objects.select_related("project")


class RequisitionListCreateView(generics.ListCreateAPIView):
    """List requests visible to the user and create employee-owned drafts."""

    serializer_class = RequisitionSerializer

    def get_queryset(self):
        """Apply role-based visibility and preload related display fields."""
        return filter_queryset_by_role(
            self.request.user,
            Requisition.objects.select_related("requestor", "project", "budget_line"),
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
    """Retrieve requests and allow owners to edit only drafts or returned items."""

    serializer_class = RequisitionSerializer

    def get_queryset(self):
        """Limit detail and update lookups to requests visible to the user."""
        return filter_queryset_by_role(self.request.user, Requisition.objects.all())

    def update(self, request, *args, **kwargs):
        """Authorize full updates only for the owner of an editable request."""
        requisition = self.get_object()
        if requisition.requestor_id != request.user.pk or requisition.status not in (
            Requisition.STATUS_DRAFT,
            Requisition.STATUS_REVISION_REQUESTED,
        ):
            raise DRFPermissionDenied(
                "Only the requester can edit a draft or returned requisition."
            )
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        """Apply the same owner and state restrictions to partial updates."""
        requisition = self.get_object()
        if requisition.requestor_id != request.user.pk or requisition.status not in (
            Requisition.STATUS_DRAFT,
            Requisition.STATUS_REVISION_REQUESTED,
        ):
            raise DRFPermissionDenied(
                "Only the requester can edit a draft or returned requisition."
            )
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
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        login(request, user)
        return Response({"detail": "Authenticated", "user_id": user.pk})


class LogoutAPIView(APIView):
    """End the current authenticated API session."""

    def post(self, request):
        """Invalidate the session and return a confirmation."""
        logout(request)
        return Response({"detail": "Logged out successfully."})


class ProtectedFileAPIView(APIView):
    """Serve evidence through the same access checks as browser downloads."""

    def get(self, request, kind, pk):
        """Return a protected file after verifying request visibility."""
        return _protected_file_response(request.user, kind, pk)


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
