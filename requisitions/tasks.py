"""Asynchronous background task queue, task registry, and worker execution engine."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
from typing import Any, Callable

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.utils import timezone

from requisitions.models import BackgroundTask, Requisition, User

logger = logging.getLogger("requisitions.tasks")

TASK_REGISTRY: dict[str, Callable[..., Any]] = {}
_THREAD_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fh-bg-worker")


def register_task(name: str):
    """Decorator registering a callable in the background task registry."""

    def decorator(func: Callable[..., Any]):
        TASK_REGISTRY[name] = func
        return func

    return decorator


@register_task("send_enrollment_invitation")
def send_enrollment_invitation_task(
    *,
    user_id: int,
    temporary_password: str,
    login_url: str | None = None,
) -> bool:
    """Send an account enrollment invitation email in the background."""
    from requisitions.enrollment import send_enrollment_invitation

    user = User.objects.filter(pk=user_id).first()
    if not user or not user.email:
        return False
    sent, error = send_enrollment_invitation(
        user, temporary_password, login_url=login_url
    )
    if not sent and error:
        raise RuntimeError(error)
    return sent


@register_task("send_workflow_notification")
def send_workflow_notification_task(
    *,
    requisition_id: int,
    action: str,
    previous_status: str,
    new_status: str,
    actor_id: int,
    comments: str = "",
) -> int:
    """Notify the requester when their requisition status changes."""
    requisition = (
        Requisition.objects.select_related("requestor")
        .filter(pk=requisition_id)
        .first()
    )
    if not requisition or not requisition.requestor.email:
        return 0

    actor = User.objects.filter(pk=actor_id).first()
    actor_label = (actor.get_full_name() or actor.username) if actor else "System"
    subject = f"[FinanceDesk] Requisition #{requisition.pk} ({requisition.title}) — {requisition.get_status_display()}"
    comment_block = f"\nReviewer Comment:\n{comments}\n" if comments else ""
    body = (
        f"Hello {requisition.requestor.get_full_name() or requisition.requestor.username},\n\n"
        f"Your requisition '{requisition.title}' (#{requisition.pk}) was updated by {actor_label}.\n"
        f"Action: {action}\n"
        f"New Status: {requisition.get_status_display()}\n"
        f"{comment_block}\n"
        "Regards,\n"
        "FinanceDesk Notifications"
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@financedesk.local")
    return send_mail(
        subject=subject,
        message=body,
        from_email=from_email,
        recipient_list=[requisition.requestor.email],
        fail_silently=False,
    )


@register_task("prewarm_requisition_pdf")
def prewarm_requisition_pdf_task(*, requisition_id: int) -> int:
    """Pre-generate and cache the printable PDF form for Finance filing."""
    from requisitions.cache import get_cached_requisition_pdf
    from requisitions.pdf import build_requisition_pdf

    requisition = (
        Requisition.objects.select_related("requestor")
        .prefetch_related("items", "logs__actor")
        .filter(pk=requisition_id)
        .first()
    )
    if not requisition:
        return 0
    pdf_bytes = get_cached_requisition_pdf(
        requisition, lambda: build_requisition_pdf(requisition)
    )
    return len(pdf_bytes)


def execute_task_record(task: BackgroundTask) -> bool:
    """Execute a single BackgroundTask record and update its status/retry metadata."""
    handler = TASK_REGISTRY.get(task.task_name)
    now = timezone.now()
    task.status = BackgroundTask.STATUS_RUNNING
    task.started_at = now
    task.attempts += 1
    task.save(update_fields=["status", "started_at", "attempts"])

    if handler is None:
        task.status = BackgroundTask.STATUS_FAILED
        task.last_error = f"Unregistered task name: {task.task_name}"
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "last_error", "completed_at"])
        return False

    try:
        handler(**(task.payload or {}))
    except Exception as exc:
        logger.warning(
            "Background task %s (#%s) failed on attempt %s/%s: %s",
            task.task_name,
            task.pk,
            task.attempts,
            task.max_attempts,
            exc,
        )
        task.last_error = str(exc)
        # Re-queue with exponential backoff (30s, 60s, 120s...) until max_attempts is reached.
        if task.attempts < task.max_attempts:
            backoff_seconds = 30 * (2 ** (task.attempts - 1))
            task.status = BackgroundTask.STATUS_PENDING
            task.scheduled_at = timezone.now() + timedelta(seconds=backoff_seconds)
            task.save(update_fields=["status", "last_error", "scheduled_at"])
        else:
            task.status = BackgroundTask.STATUS_FAILED
            task.completed_at = timezone.now()
            task.save(update_fields=["status", "last_error", "completed_at"])
        return False

    task.status = BackgroundTask.STATUS_COMPLETED
    task.last_error = ""
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "last_error", "completed_at"])
    return True


def _run_task_in_thread(task_id: int) -> None:
    try:
        task = BackgroundTask.objects.filter(pk=task_id).first()
        if task and task.status == BackgroundTask.STATUS_PENDING:
            execute_task_record(task)
    finally:
        connection.close()


def enqueue_task(
    task_name: str,
    *,
    max_attempts: int = 3,
    delay_seconds: int = 0,
    mode: str | None = None,
    **payload: Any,
) -> BackgroundTask:
    """Persist a background task and dispatch it according to BACKGROUND_TASK_MODE.

    Modes:
    - ``eager`` (default in dev/tests): executes synchronously in-process
    - ``thread``: dispatches to a background thread pool after DB transaction commit
    - ``db_queue``: leaves in PENDING state for ``python manage.py run_worker``
    """
    scheduled_at = timezone.now() + timedelta(seconds=max(0, delay_seconds))
    task = BackgroundTask.objects.create(
        task_name=task_name,
        payload=payload,
        status=BackgroundTask.STATUS_PENDING,
        max_attempts=max_attempts,
        scheduled_at=scheduled_at,
    )
    effective_mode = (
        mode or getattr(settings, "BACKGROUND_TASK_MODE", "eager")
    ).strip().lower()

    if effective_mode == "eager" and delay_seconds <= 0:
        execute_task_record(task)
    elif effective_mode == "thread" and delay_seconds <= 0:
        transaction.on_commit(lambda: _THREAD_POOL.submit(_run_task_in_thread, task.pk))
    return task


def process_pending_tasks(limit: int = 50) -> tuple[int, int]:
    """Claim and execute up to ``limit`` due background tasks.

    Returns ``(completed_count, failed_count)``.
    """
    completed = 0
    failed = 0
    supports_skip_locked = getattr(
        connection.features, "has_select_for_update_skip_locked", False
    )

    for _ in range(limit):
        with transaction.atomic():
            qs = BackgroundTask.objects.filter(
                status=BackgroundTask.STATUS_PENDING,
                scheduled_at__lte=timezone.now(),
            ).order_by("scheduled_at", "id")
            if supports_skip_locked:
                qs = qs.select_for_update(skip_locked=True)
            task = qs.first()
            if task is None:
                break
            task.status = BackgroundTask.STATUS_RUNNING
            task.save(update_fields=["status"])

        if execute_task_record(task):
            completed += 1
        else:
            failed += 1

    return completed, failed
