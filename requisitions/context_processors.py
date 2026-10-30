"""Template context for role-aware portal navigation."""

from requisitions.workflow import can_access_approval_queue


def role_navigation(request):
    """Expose approval-queue visibility to shared navigation templates."""
    return {"show_approval_queue": can_access_approval_queue(request.user)}
