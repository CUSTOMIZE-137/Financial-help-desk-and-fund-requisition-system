"""Requisition-specific authorization, visibility, and transition services."""

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from app.workflow import ROLE_STAGE_MAP, can_transition, next_status_for_action
from requisitions.cache import get_cached_delegate_roles
from requisitions.models import Requisition, User


def active_delegate_roles(user):
    """Return roles delegated to this user whose date ranges include today."""
    if not getattr(user, "is_authenticated", False) or not user.pk:
        return []

    def _fetch():
        today = timezone.localdate()
        delegated_approvers = User.objects.filter(
            delegate=user,
            is_active=True,
            delegate_start_date__lte=today,
            delegate_end_date__gte=today,
        )
        return list(delegated_approvers.values_list("role", "department"))

    return get_cached_delegate_roles(user, _fetch)


def can_access_approval_queue(user):
    """Return whether the user has an assigned approval role, directly or by delegation."""
    if not user.is_authenticated:
        return False
    if user.role == User.ROLE_EMPLOYEE:
        return any(role in ROLE_STAGE_MAP for role, _ in active_delegate_roles(user))
    if user.is_superuser or user.role == User.ROLE_ADMIN:
        return True
    if user.role in ROLE_STAGE_MAP:
        return True
    return any(role in ROLE_STAGE_MAP for role, _ in active_delegate_roles(user))


def can_download_requisition_form(user, requisition=None, delegate_roles=None):
    """Return whether the user may download printable requisition forms for filing."""
    if not user.is_authenticated:
        return False
    if user.is_superuser or user.role in (User.ROLE_FINANCE, User.ROLE_ADMIN):
        has_finance_access = True
    else:
        if delegate_roles is None:
            delegate_roles = active_delegate_roles(user)
        has_finance_access = any(role == User.ROLE_FINANCE for role, _ in delegate_roles)
    if not has_finance_access:
        return False
    if requisition is None:
        return True
    return filter_queryset_by_role(
        user, Requisition.objects.filter(pk=requisition.pk)
    ).exists()


def can_execute_action(user, requisition, action, delegate_roles=None):
    """Check user, role, department, and delegation rules for an action."""
    if not user.is_authenticated or not can_transition(requisition.status, action):
        return False
    if action in ("SUBMIT", "RESUBMIT", "SUBMIT_ACCOUNTABILITY"):
        return requisition.requestor_id == user.pk

    required_status = ROLE_STAGE_MAP.get(user.role)
    if required_status == requisition.status:
        if user.role == User.ROLE_SUPERVISOR:
            return bool(
                not requisition.is_administrative
                and user.department
                and user.department == requisition.department
            )
        return True

    if delegate_roles is None:
        delegate_roles = active_delegate_roles(user)

    for role, department in delegate_roles:
        if ROLE_STAGE_MAP.get(role) != requisition.status:
            continue
        if role == User.ROLE_SUPERVISOR and (
            requisition.is_administrative
            or not department
            or department != requisition.department
        ):
            continue
        return True

    return False


def filter_queryset_by_role(user, queryset):
    """Restrict requests to those visible to the user or their active delegates."""
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser or user.role == User.ROLE_ADMIN:
        return queryset

    visible = Q(requestor=user) | Q(logs__actor=user)
    if user.role == User.ROLE_SUPERVISOR and user.department:
        # Administrative activities bypass the supervisor and go directly to finance.
        visible |= Q(department=user.department, activity_type=Requisition.ACTIVITY_NON_ADMINISTRATIVE)
    if user.role == User.ROLE_ME_OFFICER:
        visible |= Q(status=Requisition.STATUS_PENDING_ME)
    elif user.role == User.ROLE_PROGRAM_LEAD:
        visible |= Q(status=Requisition.STATUS_PENDING_PROGRAM_LEAD)
    elif user.role == User.ROLE_EXECUTIVE_DIRECTOR:
        visible |= Q(status=Requisition.STATUS_PENDING_EXECUTIVE_DIRECTOR)
    elif user.role == User.ROLE_FINANCE:
        visible |= Q(
            status__in=[
                Requisition.STATUS_PENDING_FINANCE,
                Requisition.STATUS_DISBURSED,
                Requisition.STATUS_AWAITING_ACCOUNTABILITY,
                Requisition.STATUS_COMPLETED,
            ]
        )
    elif user.role == User.ROLE_INTERNAL_AUDIT:
        visible |= Q(status=Requisition.STATUS_AWAITING_ACCOUNTABILITY)
    for role, department in active_delegate_roles(user):
        if role == User.ROLE_FINANCE:
            visible |= Q(
                status__in=[
                    Requisition.STATUS_PENDING_FINANCE,
                    Requisition.STATUS_DISBURSED,
                    Requisition.STATUS_AWAITING_ACCOUNTABILITY,
                    Requisition.STATUS_COMPLETED,
                ]
            )
            continue
        stage = ROLE_STAGE_MAP.get(role)
        if stage:
            condition = Q(status=stage)
            if role == User.ROLE_SUPERVISOR:
                if department:
                    condition &= Q(
                        department=department,
                        activity_type=Requisition.ACTIVITY_NON_ADMINISTRATIVE,
                    )
                else:
                    continue
            visible |= condition
    return queryset.filter(visible).distinct()


def transition_requisition(
    requisition_id,
    actor,
    action,
    comments="",
    disbursement_method="",
    budget_line_id="",
):
    """Apply one authorized state transition and persist its audit record.

    The transaction keeps the request, accountability, and audit history
    consistent if any validation or database write fails.
    """
    from django.core.exceptions import PermissionDenied, ValidationError
    from django.db import transaction
    from django.utils import timezone

    with transaction.atomic():
        # Lock the request so concurrent approvals cannot both advance its state.
        requisition = (
            Requisition.objects.select_for_update()
            .select_related("requestor")
            .get(pk=requisition_id)
        )
        if not can_execute_action(actor, requisition, action):
            raise PermissionDenied("You are not authorized to perform this action.")
        if action not in ("SUBMIT", "RESUBMIT") and not comments.strip():
            if action in ("REQUEST_REVISION", "REJECT"):
                raise ValidationError("A comment is required for this action.")

        previous_status = requisition.status
        is_admin_activity = (requisition.activity_type == Requisition.ACTIVITY_ADMINISTRATIVE)
        new_status = next_status_for_action(
            previous_status, action, is_administrative=is_admin_activity
        )
        if new_status is None:
            raise ValidationError("This action is not valid for the current status.")

        # Budget-line selection was removed from the model layer; keep the old
        # parameter only for backwards compatibility with older callers.
        _ = budget_line_id

        if action == "SUBMIT":
            requisition.full_clean()
        if action == "APPROVE" and previous_status == Requisition.STATUS_PENDING_FINANCE:
            valid_methods = dict(Requisition.DISBURSEMENT_METHOD_CHOICES)
            if disbursement_method not in valid_methods:
                raise ValidationError("Select a valid disbursement method before disbursing funds.")
            requisition.disbursement_method = disbursement_method
            requisition.voucher_number = f"FV-{timezone.localdate():%Y}-{requisition.pk:06d}"
            requisition.disbursed_at = timezone.now()
            if not requisition.approved_at:
                requisition.approved_at = timezone.now()
        if (
            action == "APPROVE"
            and previous_status == Requisition.STATUS_PENDING_EXECUTIVE_DIRECTOR
        ):
            requisition.approved_at = timezone.now()
        if (
            action == "SUBMIT_ACCOUNTABILITY"
            and not requisition.accountability_submissions.exists()
        ):
            raise ValidationError(
                "Submit accountability evidence before completing this action."
            )
        if action == "FINALIZE_ACCOUNTABILITY":
            submission = requisition.accountability_submissions.first()
            if submission is None:
                raise ValidationError("No accountability submission exists for this requisition.")
            submission.audit_approved_at = timezone.now()
            submission.save(update_fields=["audit_approved_at"])

        requisition.status = new_status
        requisition.last_comment = comments.strip()
        if action == "REJECT":
            requisition.rejection_reason = comments.strip()
        requisition.save()
        # Keep the immutable event alongside the state change in the same transaction.
        requisition.logs.create(
            actor=actor,
            action=action,
            previous_status=previous_status,
            new_status=new_status,
            comments=comments.strip(),
        )
        if new_status in (
            Requisition.STATUS_PENDING_FINANCE,
            Requisition.STATUS_DISBURSED,
        ):
            from requisitions.tasks import enqueue_task

            enqueue_task("prewarm_requisition_pdf", requisition_id=requisition.pk)
        if getattr(settings, "WORKFLOW_EMAIL_NOTIFICATIONS", False):
            from requisitions.tasks import enqueue_task

            enqueue_task(
                "send_workflow_notification",
                requisition_id=requisition.pk,
                action=action,
                previous_status=previous_status,
                new_status=new_status,
                actor_id=actor.pk,
                comments=comments.strip(),
            )
        return requisition
