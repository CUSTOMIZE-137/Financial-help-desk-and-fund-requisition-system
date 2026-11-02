from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from requisitions.models import AccountabilitySubmission, Requisition, User
from requisitions.workflow import can_execute_action, transition_requisition


class RequisitionWorkflowTests(TestCase):
    """Verify the project-free requisition workflow remains valid end to end."""

    def setUp(self):
        self.employee = User.objects.create_user(
            username="employee",
            email="employee@example.com",
            password="test-password-123",
            role=User.ROLE_EMPLOYEE,
            department="Programs",
        )
        self.supervisor = User.objects.create_user(
            username="supervisor",
            email="supervisor@example.com",
            password="test-password-123",
            role=User.ROLE_SUPERVISOR,
            department="Programs",
        )
        self.me_officer = User.objects.create_user(
            username="me-officer",
            email="me@example.com",
            password="test-password-123",
            role=User.ROLE_ME_OFFICER,
        )
        self.director = User.objects.create_user(
            username="director",
            email="director@example.com",
            password="test-password-123",
            role=User.ROLE_PROGRAM_DIRECTOR,
        )
        self.finance = User.objects.create_user(
            username="finance",
            email="finance@example.com",
            password="test-password-123",
            role=User.ROLE_FINANCE,
        )
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="test-password",
            role=User.ROLE_ADMIN,
        )
        self.superuser = User.objects.create_superuser(
            username="superuser",
            email="superuser@example.com",
            password="test-password",
        )
        self.internal_auditor = User.objects.create_user(
            username="internal-auditor",
            email="audit@example.com",
            password="test-password-123",
            role=User.ROLE_INTERNAL_AUDIT,
        )

        self.requisition = Requisition.objects.create(
            requestor=self.employee,
            title="Community workshop",
            description="Run a community workshop.",
            amount=Decimal("175.00"),
            currency="UGX",
            department="Programs",
            indicator="Number of participants reached",
            activity_date=timezone.localdate() + timedelta(days=7),
            program="Community Outreach",
        )

    def test_non_administrative_activity_requires_indicator(self):
        """Program requests must include an indicator to pass validation."""
        requisition = Requisition(
            requestor=self.employee,
            title="Missing indicator",
            description="Activity without monitoring indicator.",
            amount=Decimal("50.00"),
            currency="UGX",
            department="Programs",
            activity_type=Requisition.ACTIVITY_NON_ADMINISTRATIVE,
            indicator="",
        )

        with self.assertRaises(ValidationError):
            requisition.full_clean()

    def test_full_workflow_advances_without_project_or_budget_line(self):
        """Approve a requisition from submission through finance disbursement."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR)
        for unauthorized_approver in (
            self.me_officer,
            self.director,
            self.finance,
            self.admin,
            self.superuser,
        ):
            self.assertFalse(
                can_execute_action(unauthorized_approver, self.requisition, "APPROVE")
            )
        with self.assertRaises(PermissionDenied):
            transition_requisition(self.requisition.pk, self.finance, "APPROVE")

        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_ME)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.director, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.finance, self.requisition, "APPROVE"))

        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_DIRECTOR)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.director, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.finance, self.requisition, "APPROVE"))

        transition_requisition(self.requisition.pk, self.director, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_FINANCE)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.director, self.requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.finance, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.admin, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.superuser, self.requisition, "APPROVE"))

        transition_requisition(
            self.requisition.pk,
            self.finance,
            "APPROVE",
            comments="Funds approved.",
            disbursement_method=Requisition.DISBURSEMENT_BANK_TRANSFER,
        )
        self.requisition.refresh_from_db()

        self.assertEqual(self.requisition.status, Requisition.STATUS_DISBURSED)
        self.assertEqual(
            self.requisition.disbursement_method,
            Requisition.DISBURSEMENT_BANK_TRANSFER,
        )
        self.assertTrue(self.requisition.voucher_number.startswith("FV-"))

    def test_administrative_activity_routes_directly_to_finance_only(self):
        """Only administrative activities skip the program approval stages."""
        requisition = Requisition.objects.create(
            requestor=self.employee,
            title="Office supplies",
            description="Administrative supplies.",
            amount=Decimal("75.00"),
            currency="UGX",
            department="Programs",
            activity_type=Requisition.ACTIVITY_ADMINISTRATIVE,
        )

        transition_requisition(requisition.pk, self.employee, "SUBMIT")
        requisition.refresh_from_db()
        self.assertEqual(requisition.status, Requisition.STATUS_PENDING_FINANCE)
        self.assertTrue(can_execute_action(self.finance, requisition, "APPROVE"))
        for unauthorized_approver in (
            self.supervisor,
            self.me_officer,
            self.director,
            self.admin,
            self.superuser,
        ):
            self.assertFalse(
                can_execute_action(unauthorized_approver, requisition, "APPROVE")
            )
            with self.assertRaises(PermissionDenied):
                transition_requisition(
                    requisition.pk, unauthorized_approver, "APPROVE"
                )

        transition_requisition(
            requisition.pk,
            self.finance,
            "APPROVE",
            disbursement_method=Requisition.DISBURSEMENT_PETTY_CASH,
        )
        requisition.refresh_from_db()
        self.assertEqual(requisition.status, Requisition.STATUS_DISBURSED)

    def test_administrative_resubmission_returns_directly_to_finance(self):
        """Administrative requests remain on the Finance-only route after revision."""
        requisition = Requisition.objects.create(
            requestor=self.employee,
            title="Office supplies",
            description="Administrative supplies.",
            amount=Decimal("75.00"),
            currency="UGX",
            department="Programs",
            activity_type=Requisition.ACTIVITY_ADMINISTRATIVE,
        )
        transition_requisition(requisition.pk, self.employee, "SUBMIT")
        transition_requisition(
            requisition.pk,
            self.finance,
            "REQUEST_REVISION",
            "Please update the estimate.",
        )
        transition_requisition(requisition.pk, self.employee, "RESUBMIT")
        requisition.refresh_from_db()
        self.assertEqual(requisition.status, Requisition.STATUS_PENDING_FINANCE)
        self.assertFalse(can_execute_action(self.supervisor, requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.finance, requisition, "APPROVE"))

    def test_revision_requires_comment_and_resubmission(self):
        """Review comments are mandatory for revision requests and resubmission restarts the review."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")

        with self.assertRaises(ValidationError):
            transition_requisition(
                self.requisition.pk,
                self.supervisor,
                "REQUEST_REVISION",
            )

        transition_requisition(
            self.requisition.pk,
            self.supervisor,
            "REQUEST_REVISION",
            "Please update the activity plan.",
        )
        transition_requisition(
            self.requisition.pk,
            self.employee,
            "RESUBMIT",
            "Updated the plan.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR)

    def test_accountability_submission_and_internal_audit_close(self):
        """Disbursed requests can be liquidated and closed by Internal Audit."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.director, "APPROVE")
        transition_requisition(
            self.requisition.pk,
            self.finance,
            "APPROVE",
            disbursement_method=Requisition.DISBURSEMENT_BANK_TRANSFER,
        )

        submission = AccountabilitySubmission.objects.create(
            requisition=self.requisition,
            submitted_by=self.employee,
            actual_amount_spent=Decimal("120.00"),
            activity_report="Workshop completed; 25 beneficiaries attended.",
            receipt_reference="RCPT-001",
        )

        transition_requisition(
            self.requisition.pk,
            self.employee,
            "SUBMIT_ACCOUNTABILITY",
            "Receipts submitted.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_AWAITING_ACCOUNTABILITY)

        transition_requisition(
            self.requisition.pk,
            self.internal_auditor,
            "FINALIZE_ACCOUNTABILITY",
            "Balance reconciled.",
        )
        self.requisition.refresh_from_db()
        submission.refresh_from_db()

        self.assertEqual(self.requisition.status, Requisition.STATUS_COMPLETED)
        self.assertIsNotNone(submission.audit_approved_at)
        self.assertEqual(self.requisition.logs.count(), 7)

    def test_views_hide_approval_controls_from_employees(self):
        """Employees may view their request but cannot see reviewer controls."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")

        self.client.force_login(self.employee)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Community workshop")

        detail = self.client.get(reverse("requisition-detail", args=[self.requisition.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'value="APPROVE"')
        self.assertNotContains(detail, 'value="REQUEST_REVISION"')

        queue = self.client.get(reverse("approval-queue"))
        self.assertEqual(queue.status_code, 403)

        self.client.force_login(self.supervisor)
        queue = self.client.get(reverse("approval-queue"))
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, "Community workshop")

    def test_api_lists_visible_requisitions_for_a_logged_in_user(self):
        """The REST API returns only the requisitions the current user can see."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")

        self.client.force_login(self.employee)
        response = self.client.get("/api/requisitions/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(any(item["title"] == "Community workshop" for item in data))

        self.client.force_login(self.supervisor)
        response = self.client.get("/api/requisitions/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(item["title"] == "Community workshop" for item in response.json()))
