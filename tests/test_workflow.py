"""Unit tests for state transitions and role-level workflow rules."""

from django.test import SimpleTestCase

from app.workflow import (
    build_audit_payload,
    can_transition,
    next_status_for_action,
    normalize_role,
    validate_action_permission,
)


class WorkflowRulesTests(SimpleTestCase):
    """Verify the framework-independent workflow decision helpers."""

    def test_normalize_role_handles_case_and_spacing(self):
        """Normalize case, surrounding whitespace, and multiword role names."""
        self.assertEqual(normalize_role(" employee "), "EMPLOYEE")
        self.assertEqual(normalize_role("Program Lead"), "PROGRAM_LEAD")
        self.assertEqual(normalize_role("Executive Director"), "EXECUTIVE_DIRECTOR")

    def test_can_transition_approves_to_next_stage(self):
        """Map approvals to the next sequential stage, including finance."""
        self.assertTrue(can_transition("PENDING_SUPERVISOR", "APPROVE"))
        self.assertEqual(
            next_status_for_action("PENDING_SUPERVISOR", "APPROVE"),
            "PENDING_ME",
        )
        self.assertEqual(
            next_status_for_action("PENDING_ME", "APPROVE"),
            "PENDING_PROGRAM_LEAD",
        )
        self.assertEqual(
            next_status_for_action("PENDING_PROGRAM_LEAD", "APPROVE"),
            "PENDING_EXECUTIVE_DIRECTOR",
        )
        self.assertEqual(
            next_status_for_action("PENDING_EXECUTIVE_DIRECTOR", "APPROVE"),
            "PENDING_FINANCE",
        )
        self.assertEqual(
            next_status_for_action("PENDING_FINANCE", "APPROVE"),
            "DISBURSED",
        )

    def test_can_transition_rejects_invalid_stage_action_combos(self):
        """Reject actions that are absent from a state's transition table."""
        self.assertFalse(can_transition("DRAFT", "APPROVE"))
        self.assertFalse(can_transition("COMPLETED", "SUBMIT"))
        self.assertIsNone(next_status_for_action("DISBURSED", "APPROVE"))

    def test_validate_action_permission_for_roles(self):
        """Permit assigned roles and deny roles attempting another stage."""
        self.assertTrue(
            validate_action_permission("SUPERVISOR", "PENDING_SUPERVISOR", "APPROVE")
        )
        self.assertTrue(
            validate_action_permission("ME_OFFICER", "PENDING_ME", "APPROVE")
        )
        self.assertTrue(
            validate_action_permission("PROGRAM_LEAD", "PENDING_PROGRAM_LEAD", "APPROVE")
        )
        self.assertTrue(
            validate_action_permission(
                "EXECUTIVE_DIRECTOR", "PENDING_EXECUTIVE_DIRECTOR", "APPROVE"
            )
        )
        self.assertTrue(
            validate_action_permission(
                "EXECUTIVE_DIRECTOR", "PENDING_EXECUTIVE_DIRECTOR", "REQUEST_REVISION"
            )
        )
        self.assertTrue(
            validate_action_permission(
                "EXECUTIVE_DIRECTOR", "PENDING_EXECUTIVE_DIRECTOR", "REJECT"
            )
        )
        self.assertTrue(
            validate_action_permission("EMPLOYEE", "DRAFT", "SUBMIT")
        )
        self.assertTrue(
            validate_action_permission("FINANCE", "PENDING_FINANCE", "APPROVE")
        )
        self.assertTrue(
            validate_action_permission(
                "INTERNAL_AUDIT", "AWAITING_ACCOUNTABILITY", "FINALIZE_ACCOUNTABILITY"
            )
        )
        self.assertTrue(
            validate_action_permission(
                "INTERNAL_AUDIT", "AWAITING_ACCOUNTABILITY", "REQUEST_REVISION"
            )
        )
        self.assertEqual(
            next_status_for_action("AWAITING_ACCOUNTABILITY", "REQUEST_REVISION"),
            "DISBURSED",
        )
        self.assertFalse(
            validate_action_permission(
                "FINANCE", "AWAITING_ACCOUNTABILITY", "FINALIZE_ACCOUNTABILITY"
            )
        )
        self.assertFalse(
            validate_action_permission(
                "FINANCE", "AWAITING_ACCOUNTABILITY", "REQUEST_REVISION"
            )
        )
        self.assertFalse(
            validate_action_permission("EMPLOYEE", "PENDING_FINANCE", "APPROVE")
        )
        self.assertFalse(
            validate_action_permission("FINANCE", "PENDING_SUPERVISOR", "APPROVE")
        )
        self.assertFalse(
            validate_action_permission("ADMIN", "PENDING_SUPERVISOR", "APPROVE")
        )
        self.assertFalse(
            validate_action_permission("ADMIN", "PENDING_FINANCE", "APPROVE")
        )

    def test_build_audit_payload_strips_comments(self):
        """Trim free-form comments before they are stored with an action."""
        self.assertEqual(
            build_audit_payload(12, 4, "DRAFT", "SUBMIT", "PENDING_SUPERVISOR", "  Sent  "),
            {
                "requisition_id": 12,
                "actor_id": 4,
                "previous_status": "DRAFT",
                "action": "SUBMIT",
                "new_status": "PENDING_SUPERVISOR",
                "comments": "Sent",
            },
        )
