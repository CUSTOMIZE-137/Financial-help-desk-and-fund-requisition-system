"""Integration tests for request permissions, accounting, pages, and API."""

from datetime import timedelta
from decimal import Decimal
import tempfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.test import override_settings
from rest_framework.test import APIClient

from requisitions.models import (
    AccountabilitySubmission,
    BudgetLine,
    Project,
    Requisition,
    RequisitionLog,
    User,
)
from requisitions.workflow import can_execute_action, transition_requisition


class RequisitionWorkflowTests(TestCase):
    """Exercise complete request workflows against the test database."""

    def setUp(self):
        """Create a request, budget, project, and users for each workflow role."""
        self.employee = User.objects.create_user(
            username="employee",
            password="test-password-123",
            role=User.ROLE_EMPLOYEE,
            department="Programs",
        )
        self.supervisor = User.objects.create_user(
            username="supervisor",
            password="test-password-123",
            role=User.ROLE_SUPERVISOR,
            department="Programs",
        )
        self.me_officer = User.objects.create_user(
            username="me",
            password="test-password-123",
            role=User.ROLE_ME_OFFICER,
        )
        self.director = User.objects.create_user(
            username="director",
            password="test-password-123",
            role=User.ROLE_PROGRAM_DIRECTOR,
        )
        self.finance = User.objects.create_user(
            username="finance",
            password="test-password-123",
            role=User.ROLE_FINANCE,
        )
        self.project = Project.objects.create(code="P-1", name="Community Program")
        self.budget_line = BudgetLine.objects.create(
            project=self.project,
            code="BL-1",
            category="Field activities",
            allocated_amount=Decimal("1000.00"),
            currency="USD",
        )
        self.requisition = Requisition.objects.create(
            requestor=self.employee,
            project=self.project,
            budget_line=self.budget_line,
            title="Field workshop",
            description="Run a community workshop.",
            amount=Decimal("120.00"),
            currency="USD",
            department="Programs",
        )

    def test_full_workflow_and_budget_reconciliation(self):
        """Approve, disburse, liquidate, and reconcile a request end to end."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.director, "APPROVE")
        transition_requisition(self.requisition.pk, self.finance, "APPROVE")

        self.budget_line.refresh_from_db()
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_DISBURSED)
        self.assertEqual(self.budget_line.spent_amount, Decimal("120.00"))

        AccountabilitySubmission.objects.create(
            requisition=self.requisition,
            submitted_by=self.employee,
            actual_amount_spent=Decimal("100.00"),
            activity_report="Workshop completed; 25 participants attended.",
        )
        transition_requisition(
            self.requisition.pk,
            self.employee,
            "SUBMIT_ACCOUNTABILITY",
            "Receipts and report attached.",
        )
        transition_requisition(
            self.requisition.pk,
            self.finance,
            "FINALIZE_ACCOUNTABILITY",
            "Balance reconciled.",
        )

        self.requisition.refresh_from_db()
        self.budget_line.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_COMPLETED)
        self.assertEqual(self.budget_line.spent_amount, Decimal("100.00"))
        self.assertEqual(self.requisition.logs.count(), 7)

    def test_revision_requires_a_comment_and_returns_to_requester(self):
        """Require revision context and return resubmitted requests to review."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        with self.assertRaises(ValidationError):
            transition_requisition(
                self.requisition.pk, self.supervisor, "REQUEST_REVISION"
            )
        transition_requisition(
            self.requisition.pk,
            self.supervisor,
            "REQUEST_REVISION",
            "Please attach the activity TOR.",
        )
        transition_requisition(
            self.requisition.pk, self.employee, "RESUBMIT", "Updated the request."
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR)

    def test_me_revision_restarts_approval_from_supervisor(self):
        """Restart review at the supervisor after M&E requests a revision."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_ME)

        transition_requisition(
            self.requisition.pk,
            self.me_officer,
            "REQUEST_REVISION",
            "Please update the activity plan.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(
            self.requisition.status, Requisition.STATUS_REVISION_REQUESTED
        )

        transition_requisition(self.requisition.pk, self.employee, "RESUBMIT")
        self.requisition.refresh_from_db()
        self.assertEqual(
            self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR
        )
        self.assertTrue(
            can_execute_action(self.supervisor, self.requisition, "APPROVE")
        )
        self.assertFalse(
            can_execute_action(self.me_officer, self.requisition, "APPROVE")
        )

        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_ME)
        self.assertTrue(
            can_execute_action(self.me_officer, self.requisition, "APPROVE")
        )

    def test_revision_can_only_be_resubmitted_by_the_original_requester(self):
        """Return every review-stage revision to its original requester."""
        reviewer_stages = (
            (self.supervisor, Requisition.STATUS_PENDING_SUPERVISOR),
            (self.me_officer, Requisition.STATUS_PENDING_ME),
            (self.director, Requisition.STATUS_PENDING_DIRECTOR),
            (self.finance, Requisition.STATUS_PENDING_FINANCE),
        )
        other_employee = User.objects.create_user(
            username="other-employee", role=User.ROLE_EMPLOYEE
        )
        administrator = User.objects.create_user(
            username="administrator", role=User.ROLE_ADMIN
        )

        for reviewer, stage in reviewer_stages:
            with self.subTest(stage=stage):
                Requisition.objects.filter(pk=self.requisition.pk).update(status=stage)
                transition_requisition(
                    self.requisition.pk,
                    reviewer,
                    "REQUEST_REVISION",
                    "Please correct the request.",
                )
                self.requisition.refresh_from_db()
                self.assertEqual(
                    self.requisition.status, Requisition.STATUS_REVISION_REQUESTED
                )
                self.assertTrue(
                    can_execute_action(self.employee, self.requisition, "RESUBMIT")
                )
                self.assertFalse(
                    can_execute_action(other_employee, self.requisition, "RESUBMIT")
                )
                self.assertFalse(
                    can_execute_action(administrator, self.requisition, "RESUBMIT")
                )
                transition_requisition(
                    self.requisition.pk, self.employee, "RESUBMIT"
                )
                self.requisition.refresh_from_db()
                self.assertEqual(
                    self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR
                )

    def test_only_assigned_role_can_approve_stage(self):
        """Reject a finance user attempting to approve at the employee stage."""
        self.assertFalse(
            can_execute_action(self.finance, self.requisition, "APPROVE")
        )
        with self.assertRaises(PermissionDenied):
            transition_requisition(self.requisition.pk, self.finance, "APPROVE")

    def test_supervisor_must_match_request_department(self):
        """Prevent supervisors from approving requests in another department."""
        other_supervisor = User.objects.create_user(
            username="other-supervisor",
            password="test-password-123",
            role=User.ROLE_SUPERVISOR,
            department="Finance",
        )
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.requisition.refresh_from_db()
        self.assertFalse(
            can_execute_action(other_supervisor, self.requisition, "APPROVE")
        )

    def test_active_delegate_can_approve_and_expired_delegate_cannot(self):
        """Honor a delegate only while the delegator's date range is active."""
        delegate = User.objects.create_user(
            username="delegate", password="test-password-123", role=User.ROLE_EMPLOYEE
        )
        today = timezone.localdate()
        self.supervisor.delegate = delegate
        self.supervisor.delegate_start_date = today
        self.supervisor.delegate_end_date = today
        self.supervisor.save()
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.requisition.refresh_from_db()
        self.assertTrue(can_execute_action(delegate, self.requisition, "APPROVE"))
        self.supervisor.delegate_end_date = today - timedelta(days=1)
        self.supervisor.save()
        self.assertFalse(can_execute_action(delegate, self.requisition, "APPROVE"))

    def test_disbursement_rechecks_budget_balance(self):
        """Recheck available funds at disbursement and leave state unchanged on failure."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.director, "APPROVE")
        self.budget_line.spent_amount = Decimal("950.00")
        self.budget_line.save(update_fields=["spent_amount"])
        with self.assertRaises(ValidationError):
            transition_requisition(self.requisition.pk, self.finance, "APPROVE")
        self.budget_line.refresh_from_db()
        self.requisition.refresh_from_db()
        self.assertEqual(self.budget_line.spent_amount, Decimal("950.00"))
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_FINANCE)

    def test_audit_entries_cannot_be_edited_or_deleted(self):
        """Keep workflow audit entries immutable through model operations."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        log = RequisitionLog.objects.get(requisition=self.requisition)
        with self.assertRaises(ValidationError):
            log.save()
        with self.assertRaises(ValidationError):
            log.delete()

    def test_dashboard_and_detail_require_login_and_hide_other_requests(self):
        """Require sign-in and hide request details from unrelated employees."""
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.employee)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Field workshop")
        response = self.client.get(
            reverse("requisition-detail", args=[self.requisition.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Submit")
        self.assertNotContains(response, 'value="APPROVE"')
        self.assertNotContains(response, 'value="REQUEST_REVISION"')
        self.assertNotContains(response, 'value="REJECT"')

        stranger = User.objects.create_user(
            username="stranger", password="test-password-123", role=User.ROLE_EMPLOYEE
        )
        self.client.force_login(stranger)
        response = self.client.get(
            reverse("requisition-detail", args=[self.requisition.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_employee_portal_hides_approval_actions_and_queue(self):
        """Keep review controls and the approval queue out of employee views."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.client.force_login(self.employee)

        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, "Approval queue")

        detail = self.client.get(
            reverse("requisition-detail", args=[self.requisition.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'value="REQUEST_REVISION"')
        self.assertNotContains(detail, 'value="REJECT"')

        queue = self.client.get(reverse("approval-queue"))
        self.assertEqual(queue.status_code, 403)

    def test_employee_role_never_gets_reviewer_controls_even_if_superuser(self):
        """Keep employee permissions limited even if an account is superuser."""
        employee_admin = User.objects.create_superuser(
            username="employee-admin",
            email="employee-admin@example.com",
        )
        employee_admin.role = User.ROLE_EMPLOYEE
        employee_admin.save(update_fields=["role"])
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.client.force_login(employee_admin)

        detail = self.client.get(
            reverse("requisition-detail", args=[self.requisition.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'value="APPROVE"')
        self.assertNotContains(detail, 'value="REQUEST_REVISION"')
        self.assertNotContains(detail, 'value="REJECT"')
        self.assertEqual(
            self.client.get(reverse("approval-queue")).status_code, 403
        )

    def test_employee_cannot_request_revision_or_reject_submitted_request(self):
        """Enforce approver-only revision and rejection actions on the server."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.client.force_login(self.employee)

        for action in ("REQUEST_REVISION", "REJECT"):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("requisition-action", args=[self.requisition.pk]),
                    {"action": action, "comments": "Not authorized."},
                )
                self.assertEqual(response.status_code, 302)
                self.requisition.refresh_from_db()
                self.assertEqual(
                    self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR
                )

        self.assertEqual(self.requisition.logs.count(), 1)

        api_client = APIClient()
        api_client.force_authenticate(user=self.employee)
        for action in ("REQUEST_REVISION", "REJECT"):
            with self.subTest(api_action=action):
                response = api_client.post(
                    f"/api/requisitions/{self.requisition.pk}/transition/",
                    {"action": action, "comments": "Not authorized."},
                    format="json",
                )
                self.assertEqual(response.status_code, 403)

    def test_assigned_approver_sees_review_actions(self):
        """Keep revision and rejection controls available to assigned approvers."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.client.force_login(self.supervisor)

        detail = self.client.get(
            reverse("requisition-detail", args=[self.requisition.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Request Revision")
        self.assertContains(detail, "Reject")

    def test_approval_returns_reviewer_to_queue_after_request_advances(self):
        """Avoid redirecting reviewers to a request hidden at its next stage."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        self.client.force_login(self.me_officer)

        response = self.client.post(
            reverse("requisition-action", args=[self.requisition.pk]),
            {"action": "APPROVE"},
        )

        self.assertRedirects(
            response, reverse("approval-queue"), fetch_redirect_response=False
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_DIRECTOR)
        queue = self.client.get(response.url)
        self.assertEqual(queue.status_code, 200)
        self.assertNotContains(queue, self.requisition.title)

    def test_approval_queue_shows_all_review_actions_for_each_approver_role(self):
        """Render approve, revision, and reject controls in each assigned queue."""
        reviewer_stages = (
            (self.supervisor, Requisition.STATUS_PENDING_SUPERVISOR),
            (self.me_officer, Requisition.STATUS_PENDING_ME),
            (self.director, Requisition.STATUS_PENDING_DIRECTOR),
            (self.finance, Requisition.STATUS_PENDING_FINANCE),
        )
        for reviewer, stage in reviewer_stages:
            with self.subTest(role=reviewer.role):
                Requisition.objects.filter(pk=self.requisition.pk).update(status=stage)
                self.client.force_login(reviewer)
                response = self.client.get(reverse("approval-queue"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'value="APPROVE"')
                self.assertContains(response, 'value="REQUEST_REVISION"')
                self.assertContains(response, 'value="REJECT"')

    def test_employee_can_create_and_submit_a_draft_from_the_web(self):
        """Create a draft through the browser form and submit it for review."""
        self.client.force_login(self.employee)
        response = self.client.post(
            reverse("requisition-create"),
            {
                "project": self.project.pk,
                "budget_line": self.budget_line.pk,
                "title": "New web request",
                "description": "A request created using the web form.",
                "amount": "75.00",
                "currency": "USD",
                "due_date": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        requisition = Requisition.objects.get(title="New web request")
        self.assertEqual(requisition.requestor, self.employee)
        self.assertEqual(requisition.status, Requisition.STATUS_DRAFT)
        detail = self.client.get(
            reverse("requisition-detail", args=[requisition.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'value="SUBMIT"')
        self.assertNotContains(detail, 'value="APPROVE"')
        self.assertNotContains(detail, 'value="REQUEST_REVISION"')
        self.assertNotContains(detail, 'value="REJECT"')

        response = self.client.post(
            reverse("requisition-action", args=[requisition.pk]),
            {"action": "SUBMIT", "comments": ""},
        )
        self.assertEqual(response.status_code, 302)
        requisition.refresh_from_db()
        self.assertEqual(requisition.status, Requisition.STATUS_PENDING_SUPERVISOR)

    def test_login_page_and_api_list(self):
        """Render the login page and return the employee's visible API request."""
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

        self.client.force_login(self.employee)
        response = self.client.get("/api/requisitions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_login_rejects_external_next_url(self):
        """Prevent an external redirect after successful browser authentication."""
        response = self.client.post(
            "/login/?next=https://attacker.example",
            {"username": self.employee.username, "password": "test-password-123"},
        )
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_accountability_file_download_requires_requisition_access(self):
        """Serve an uploaded receipt only to users allowed to view its request."""
        self.requisition.status = Requisition.STATUS_DISBURSED
        self.requisition.save(update_fields=["status"])
        api_client = APIClient()
        api_client.force_authenticate(user=self.employee)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = api_client.post(
                    "/api/accountability/",
                    {
                        "requisition": self.requisition.pk,
                        "actual_amount_spent": "100.00",
                        "activity_report": "Workshop completed.",
                        "receipt_file": SimpleUploadedFile(
                            "receipt.pdf",
                            b"%PDF-1.4 test receipt",
                            content_type="application/pdf",
                        ),
                    },
                    format="multipart",
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertNotIn("receipt_file", response.data)
                self.assertIn("/api/files/receipt/", response.data["receipt_download_url"])

                download = api_client.get(response.data["receipt_download_url"])
                self.assertEqual(download.status_code, 200)
                self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 test receipt")

                stranger = User.objects.create_user(
                    username="file-stranger",
                    password="test-password-123",
                    role=User.ROLE_EMPLOYEE,
                )
                api_client.force_authenticate(user=stranger)
                denied = api_client.get(response.data["receipt_download_url"])
                self.assertEqual(denied.status_code, 404)

    def test_finance_queue_includes_accountability_review(self):
        """Include awaiting-accountability work in the finance action queue."""
        self.requisition.status = Requisition.STATUS_AWAITING_ACCOUNTABILITY
        self.requisition.save(update_fields=["status"])
        self.client.force_login(self.finance)
        response = self.client.get(reverse("approval-queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Field workshop")
