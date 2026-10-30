"""Requisition-specific authorization, visibility, and transition services."""

from django.db.models import Q
from django.utils import timezone

from app.workflow import ROLE_STAGE_MAP, can_transition, next_status_for_action
from requisitions.models import Requisition, User


def active_delegate_roles(user):
    """Return roles delegated to this user whose date ranges include today."""
    today = timezone.localdate()
    delegated_approvers = User.objects.filter(
        delegate=user,
        is_active=True,
        delegate_start_date__lte=today,
        delegate_end_date__gte=today,
    )
    return list(delegated_approvers.values_list("role", "department"))


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


def can_execute_action(user, requisition, action):
    """Check user, role, department, and delegation rules for an action."""
    if not user.is_authenticated or not can_transition(requisition.status, action):
        return False
    if action == "RESUBMIT":
        return requisition.requestor_id == user.pk
    if user.role == User.ROLE_EMPLOYEE:
        if action in ("SUBMIT", "SUBMIT_ACCOUNTABILITY"):
            return requisition.requestor_id == user.pk
        if action not in (
            "APPROVE",
            "REQUEST_REVISION",
            "REJECT",
            "FINALIZE_ACCOUNTABILITY",
        ):
            return False
    elif user.is_superuser or user.role == User.ROLE_ADMIN:
        return True

    if action in ("SUBMIT", "RESUBMIT", "SUBMIT_ACCOUNTABILITY"):
        if requisition.requestor_id == user.pk:
            return True
        if action == "SUBMIT_ACCOUNTABILITY" and user.role == User.ROLE_FINANCE:
            return True
        return False

    required_status = ROLE_STAGE_MAP.get(user.role)
    if required_status == requisition.status:
        if user.role == User.ROLE_SUPERVISOR:
            return bool(user.department and user.department == requisition.department)
        return True

    for role, department in active_delegate_roles(user):
        if ROLE_STAGE_MAP.get(role) != requisition.status and not (
            action == "FINALIZE_ACCOUNTABILITY"
            and role == User.ROLE_FINANCE
            and requisition.status == Requisition.STATUS_AWAITING_ACCOUNTABILITY
        ):
            continue
        if role == User.ROLE_SUPERVISOR and (
            not department or department != requisition.department
        ):
            continue
        return True

    return action == "FINALIZE_ACCOUNTABILITY" and user.role == User.ROLE_FINANCE


def filter_queryset_by_role(user, queryset):
    """Restrict requests to those visible to the user or their active delegates."""
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser or user.role == User.ROLE_ADMIN:
        return queryset

    visible = Q(requestor=user)
    if user.role == User.ROLE_SUPERVISOR and user.department:
        visible |= Q(department=user.department)
    if user.role == User.ROLE_ME_OFFICER:
        visible |= Q(status=Requisition.STATUS_PENDING_ME)
    elif user.role == User.ROLE_PROGRAM_DIRECTOR:
        visible |= Q(status=Requisition.STATUS_PENDING_DIRECTOR)
    elif user.role == User.ROLE_FINANCE:
        visible |= Q(
            status__in=[
                Requisition.STATUS_PENDING_FINANCE,
                Requisition.STATUS_DISBURSED,
                Requisition.STATUS_AWAITING_ACCOUNTABILITY,
            ]
        )
    for role, department in active_delegate_roles(user):
        stage = ROLE_STAGE_MAP.get(role)
        if stage:
            condition = Q(status=stage)
            if role == User.ROLE_FINANCE:
                condition |= Q(status=Requisition.STATUS_AWAITING_ACCOUNTABILITY)
            if role == User.ROLE_SUPERVISOR:
                if department:
                    condition &= Q(department=department)
                else:
                    continue
            visible |= condition
    return queryset.filter(visible).distinct()


def transition_requisition(requisition_id, actor, action, comments=""):
    """Apply one authorized state transition and persist its audit record.

    The transaction keeps the request, budget allocation, accountability, and
    log entry consistent if any validation or database write fails.
    """
    from django.core.exceptions import PermissionDenied, ValidationError
    from django.db import transaction
    from django.utils import timezone

    with transaction.atomic():
        # Lock the request so concurrent approvals cannot both advance its state.
        requisition = (
            Requisition.objects.select_for_update()
            .select_related("requestor", "budget_line", "project")
            .get(pk=requisition_id)
        )
        if not can_execute_action(actor, requisition, action):
            raise PermissionDenied("You are not authorized to perform this action.")
        if action not in ("SUBMIT", "RESUBMIT") and not comments.strip():
            if action in ("REQUEST_REVISION", "REJECT"):
                raise ValidationError("A comment is required for this action.")

        previous_status = requisition.status
        new_status = next_status_for_action(previous_status, action)
        if new_status is None:
            raise ValidationError("This action is not valid for the current status.")

        if action == "SUBMIT":
            requisition.full_clean()
            if requisition.amount > requisition.budget_line.available_amount:
                raise ValidationError("The selected budget line has insufficient funds.")
        if action == "APPROVE" and previous_status == Requisition.STATUS_PENDING_FINANCE:
            # Lock and recheck the allocation at the point funds are committed.
            budget_line = type(requisition.budget_line).objects.select_for_update().get(
                pk=requisition.budget_line_id
            )
            if requisition.amount > budget_line.available_amount:
                raise ValidationError("The selected budget line no longer has sufficient funds.")
            budget_line.spent_amount += requisition.amount
            budget_line.save(update_fields=["spent_amount"])
            requisition.disbursed_at = timezone.now()
        if action == "APPROVE" and previous_status == Requisition.STATUS_PENDING_DIRECTOR:
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
            submission.finance_approved_at = timezone.now()
            submission.save(update_fields=["finance_approved_at"])
            budget_line = type(requisition.budget_line).objects.select_for_update().get(
                pk=requisition.budget_line_id
            )
            budget_line.spent_amount -= requisition.amount - submission.actual_amount_spent
            budget_line.save(update_fields=["spent_amount"])

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
        return requisition
