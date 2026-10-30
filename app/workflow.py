"""State and role rules for the requisition approval workflow.

The transition table defines valid state changes; role checks separately constrain
who may request each change.
"""

import re
from typing import Optional

ROLE_PERMISSIONS = {
    "EMPLOYEE": ("SUBMIT", "RESUBMIT", "SUBMIT_ACCOUNTABILITY"),
    "SUPERVISOR": ("APPROVE", "REQUEST_REVISION", "REJECT"),
    "ME_OFFICER": ("APPROVE", "REQUEST_REVISION", "REJECT"),
    "PROGRAM_DIRECTOR": ("APPROVE", "REQUEST_REVISION", "REJECT"),
    "FINANCE": (
        "APPROVE",
        "REQUEST_REVISION",
        "REJECT",
        "SUBMIT_ACCOUNTABILITY",
        "FINALIZE_ACCOUNTABILITY",
    ),
    "ADMIN": (
        "SUBMIT",
        "APPROVE",
        "REQUEST_REVISION",
        "REJECT",
        "RESUBMIT",
        "SUBMIT_ACCOUNTABILITY",
        "FINALIZE_ACCOUNTABILITY",
    ),
}

REVIEW_START_STATUS = "PENDING_SUPERVISOR"

# A transition absent from this table is invalid, regardless of the acting role.
STATUS_FLOW = {
    "DRAFT": {"SUBMIT": REVIEW_START_STATUS},
    "PENDING_SUPERVISOR": {
        "APPROVE": "PENDING_ME",
        "REQUEST_REVISION": "REVISION_REQUESTED",
        "REJECT": "REJECTED",
    },
    "PENDING_ME": {
        "APPROVE": "PENDING_DIRECTOR",
        "REQUEST_REVISION": "REVISION_REQUESTED",
        "REJECT": "REJECTED",
    },
    "PENDING_DIRECTOR": {
        "APPROVE": "PENDING_FINANCE",
        "REQUEST_REVISION": "REVISION_REQUESTED",
        "REJECT": "REJECTED",
    },
    "PENDING_FINANCE": {
        "APPROVE": "DISBURSED",
        "REQUEST_REVISION": "REVISION_REQUESTED",
        "REJECT": "REJECTED",
    },
    "REVISION_REQUESTED": {"RESUBMIT": REVIEW_START_STATUS},
    "DISBURSED": {"SUBMIT_ACCOUNTABILITY": "AWAITING_ACCOUNTABILITY"},
    "AWAITING_ACCOUNTABILITY": {"FINALIZE_ACCOUNTABILITY": "COMPLETED"},
    "REJECTED": {},
    "COMPLETED": {},
}

ROLE_STAGE_MAP = {
    "SUPERVISOR": "PENDING_SUPERVISOR",
    "ME_OFFICER": "PENDING_ME",
    "PROGRAM_DIRECTOR": "PENDING_DIRECTOR",
    "FINANCE": "PENDING_FINANCE",
}

# Derive these sets from the workflow definition to keep validation in sync.
VALID_STATUSES = frozenset(STATUS_FLOW)
VALID_ACTIONS = frozenset(
    action for actions in STATUS_FLOW.values() for action in actions
)


def normalize_role(value: str) -> str:
    """Normalize spacing, case, and common aliases in a role name."""
    if value is None:
        raise ValueError("Role cannot be empty")
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError("Role cannot be empty")

    normalized = cleaned.upper()
    normalized = normalized.replace("&", "_")
    normalized = normalized.replace("-", "_")
    normalized = normalized.replace("/", "_")
    normalized = re.sub(r"[^A-Z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")

    aliases = {
        "ADMIN": "ADMIN",
        "EMPLOYEE": "EMPLOYEE",
        "SUPERVISOR": "SUPERVISOR",
        "ME_OFFICER": "ME_OFFICER",
        "M_E_OFFICER": "ME_OFFICER",
        "MEOFFICER": "ME_OFFICER",
        "PROGRAM_DIRECTOR": "PROGRAM_DIRECTOR",
        "PROGRAMDIRECTOR": "PROGRAM_DIRECTOR",
        "PROGRAM_DIRECTOR_": "PROGRAM_DIRECTOR",
        "FINANCE": "FINANCE",
        "FINANCE_OFFICER": "FINANCE",
        "FINANCEOFFICER": "FINANCE",
    }
    return aliases.get(normalized, normalized)


def can_transition(status: str, action: str) -> bool:
    """Return whether the workflow permits this action from the given state."""
    return status in VALID_STATUSES and action in STATUS_FLOW.get(status, {})


def next_status_for_action(status: str, action: str) -> Optional[str]:
    """Return the destination state, or ``None`` if the transition is invalid."""
    if not can_transition(status, action):
        return None
    return STATUS_FLOW[status][action]


def validate_action_permission(role: str, status: str, action: str) -> bool:
    """Check workflow-level role permissions, without user or department context."""
    try:
        normalized_role = normalize_role(role)
    except (AttributeError, ValueError):
        return False

    if action not in ROLE_PERMISSIONS.get(normalized_role, ()):
        return False
    if not can_transition(status, action):
        return False
    if normalized_role == "ADMIN":
        return True
    if action in ("SUBMIT", "RESUBMIT"):
        return normalized_role == "EMPLOYEE"
    if action == "SUBMIT_ACCOUNTABILITY":
        return normalized_role == "EMPLOYEE" or normalized_role == "FINANCE"
    if action == "FINALIZE_ACCOUNTABILITY":
        return normalized_role == "FINANCE" and status == "AWAITING_ACCOUNTABILITY"
    return ROLE_STAGE_MAP.get(normalized_role) == status


def build_audit_payload(
    requisition_id: int,
    actor_id: int,
    previous_status: str,
    action: str,
    new_status: str,
    comments: str = "",
) -> dict:
    """Build normalized status-change data for an audit-log entry."""
    return {
        "requisition_id": requisition_id,
        "actor_id": actor_id,
        "previous_status": previous_status,
        "action": action,
        "new_status": new_status,
        "comments": comments.strip(),
    }
