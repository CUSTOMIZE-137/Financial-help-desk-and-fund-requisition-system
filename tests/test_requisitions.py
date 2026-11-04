from datetime import timedelta
from decimal import Decimal
import tempfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from requisitions.models import AccountabilitySubmission, Requisition, User
from requisitions.workflow import (
    can_execute_action,
    filter_queryset_by_role,
    transition_requisition,
)


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
        self.program_lead = User.objects.create_user(
            username="program-lead",
            email="program-lead@example.com",
            password="test-password-123",
            role=User.ROLE_PROGRAM_LEAD,
        )
        self.executive_director = User.objects.create_user(
            username="executive-director",
            email="ed@example.com",
            password="test-password-123",
            role=User.ROLE_EXECUTIVE_DIRECTOR,
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

    def test_requisition_form_activity_dropdown_uses_programs_label(self):
        """The activity dropdown on the requisition form lists 'Programs' instead of 'Non-administrative Activity'."""
        self.client.force_login(self.employee)
        response = self.client.get(reverse("requisition-create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<option value="NON_ADMINISTRATIVE" selected>Programs</option>',
            html=True,
        )
        self.assertNotContains(response, "Non-administrative Activity")

    def test_full_workflow_advances_without_project_or_budget_line(self):
        """Approve a requisition from submission through finance disbursement."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_SUPERVISOR)
        for unauthorized_approver in (
            self.me_officer,
            self.program_lead,
            self.executive_director,
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
        self.assertFalse(can_execute_action(self.program_lead, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.executive_director, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.finance, self.requisition, "APPROVE"))

        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_PROGRAM_LEAD)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.program_lead, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.executive_director, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.finance, self.requisition, "APPROVE"))

        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_EXECUTIVE_DIRECTOR)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.program_lead, self.requisition, "APPROVE"))
        self.assertTrue(can_execute_action(self.executive_director, self.requisition, "APPROVE"))
        self.assertTrue(
            can_execute_action(self.executive_director, self.requisition, "REQUEST_REVISION")
        )
        self.assertTrue(can_execute_action(self.executive_director, self.requisition, "REJECT"))
        self.assertFalse(can_execute_action(self.finance, self.requisition, "APPROVE"))

        transition_requisition(self.requisition.pk, self.executive_director, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_FINANCE)
        self.assertFalse(can_execute_action(self.supervisor, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.program_lead, self.requisition, "APPROVE"))
        self.assertFalse(can_execute_action(self.executive_director, self.requisition, "APPROVE"))
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
            self.program_lead,
            self.executive_director,
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
        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        transition_requisition(self.requisition.pk, self.executive_director, "APPROVE")
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
        self.assertEqual(self.requisition.logs.count(), 8)

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

    def test_internal_audit_can_request_accountability_revision(self):
        """Internal Audit can return accountability to Disbursed for re-submission."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        transition_requisition(self.requisition.pk, self.executive_director, "APPROVE")
        transition_requisition(
            self.requisition.pk,
            self.finance,
            "APPROVE",
            disbursement_method=Requisition.DISBURSEMENT_BANK_TRANSFER,
        )
        AccountabilitySubmission.objects.create(
            requisition=self.requisition,
            submitted_by=self.employee,
            actual_amount_spent=Decimal("150.00"),
            activity_report="Initial report.",
            receipt_reference="RCPT-INIT",
        )
        transition_requisition(
            self.requisition.pk,
            self.employee,
            "SUBMIT_ACCOUNTABILITY",
            "Submitted initial accountability.",
        )

        transition_requisition(
            self.requisition.pk,
            self.internal_auditor,
            "REQUEST_REVISION",
            "Please attach missing receipt details.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_DISBURSED)
        self.assertTrue(
            can_execute_action(self.employee, self.requisition, "SUBMIT_ACCOUNTABILITY")
        )

    def test_executive_director_can_request_revision_and_reject(self):
        """Executive Director can request revision or reject at their approval stage."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(
            self.requisition.status, Requisition.STATUS_PENDING_EXECUTIVE_DIRECTOR
        )

        transition_requisition(
            self.requisition.pk,
            self.executive_director,
            "REQUEST_REVISION",
            "Clarify executive alignment.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_REVISION_REQUESTED)

        transition_requisition(self.requisition.pk, self.employee, "RESUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        transition_requisition(
            self.requisition.pk,
            self.executive_director,
            "REJECT",
            "Outside strategic scope.",
        )
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_REJECTED)
        self.assertEqual(self.requisition.rejection_reason, "Outside strategic scope.")

    def test_reviewer_retains_visibility_after_approval(self):
        """Approvers retain read-only visibility into requests they have previously reviewed."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        self.requisition.refresh_from_db()
        self.assertEqual(self.requisition.status, Requisition.STATUS_PENDING_PROGRAM_LEAD)

        visible_to_me = filter_queryset_by_role(self.me_officer, Requisition.objects.all())
        self.assertIn(self.requisition, visible_to_me)
        self.assertFalse(can_execute_action(self.me_officer, self.requisition, "APPROVE"))

    def test_delegated_supervisor_cannot_see_or_approve_administrative_requests(self):
        """Delegated supervisors only see non-administrative requests for their department."""
        today = timezone.localdate()
        delegate_user = User.objects.create_user(
            username="delegate-emp",
            email="delegate@example.com",
            password="test-password-123",
            role=User.ROLE_EMPLOYEE,
            department="Programs",
        )
        self.supervisor.delegate = delegate_user
        self.supervisor.delegate_start_date = today - timedelta(days=1)
        self.supervisor.delegate_end_date = today + timedelta(days=1)
        self.supervisor.save()

        admin_req = Requisition.objects.create(
            requestor=self.employee,
            title="Admin stationery",
            description="Stationery purchase.",
            amount=Decimal("40.00"),
            currency="UGX",
            department="Programs",
            activity_type=Requisition.ACTIVITY_ADMINISTRATIVE,
        )
        transition_requisition(admin_req.pk, self.employee, "SUBMIT")
        visible = filter_queryset_by_role(delegate_user, Requisition.objects.all())
        self.assertNotIn(admin_req, visible)
        self.assertFalse(can_execute_action(delegate_user, admin_req, "APPROVE"))

    def test_api_creates_and_updates_requisition_items_and_allows_admin_edits(self):
        """API supports nested line items and allows administrators to edit draft requests."""
        api_client = APIClient()
        api_client.force_authenticate(user=self.employee)
        create_resp = api_client.post(
            "/api/requisitions/",
            {
                "activity_type": Requisition.ACTIVITY_NON_ADMINISTRATIVE,
                "title": "API Workshop",
                "description": "Created with line items.",
                "indicator": "30 attendees",
                "items": [
                    {"item_description": "Venue hire", "quantity": "2.00", "unit_cost": "50.00"},
                    {"item_description": "Refreshments", "quantity": "10.00", "unit_cost": "5.50"},
                ],
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.data)
        self.assertEqual(create_resp.data["amount"], "155.00")
        self.assertEqual(len(create_resp.data["items"]), 2)
        req_id = create_resp.data["id"]

        api_client.force_authenticate(user=self.admin)
        patch_resp = api_client.patch(
            f"/api/requisitions/{req_id}/",
            {
                "items": [
                    {"item_description": "Venue hire updated", "quantity": "1.00", "unit_cost": "80.00"},
                ]
            },
            format="json",
        )
        self.assertEqual(patch_resp.status_code, 200, patch_resp.data)
        self.assertEqual(patch_resp.data["amount"], "80.00")
        self.assertEqual(len(patch_resp.data["items"]), 1)

        paginated_resp = api_client.get("/api/requisitions/?limit=1&offset=0")
        self.assertEqual(paginated_resp.status_code, 200)
        self.assertIn("results", paginated_resp.json())
        self.assertEqual(len(paginated_resp.json()["results"]), 1)

    def test_file_signature_validation_rejects_disguised_files(self):
        """Uploaded evidence files must match the magic bytes of their declared extension."""
        self.requisition.status = Requisition.STATUS_DISBURSED
        self.requisition.save(update_fields=["status"])
        api_client = APIClient()
        api_client.force_authenticate(user=self.employee)

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                bad_resp = api_client.post(
                    "/api/accountability/",
                    {
                        "requisition": self.requisition.pk,
                        "actual_amount_spent": "100.00",
                        "activity_report": "Completed.",
                        "receipt_file": SimpleUploadedFile(
                            "fake.pdf",
                            b"MZ-not-a-real-pdf",
                            content_type="application/pdf",
                        ),
                    },
                    format="multipart",
                )
                self.assertEqual(bad_resp.status_code, 400)

                good_resp = api_client.post(
                    "/api/accountability/",
                    {
                        "requisition": self.requisition.pk,
                        "actual_amount_spent": "100.00",
                        "activity_report": "Completed.",
                        "receipt_file": SimpleUploadedFile(
                            "valid.pdf",
                            b"%PDF-1.4 valid receipt content",
                            content_type="application/pdf",
                        ),
                    },
                    format="multipart",
                )
                self.assertEqual(good_resp.status_code, 201, good_resp.data)

    def test_finance_officer_can_download_requisition_form_for_filing(self):
        """Finance officers see the download button and can download the PDF requisition form."""
        transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
        transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
        transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
        transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
        transition_requisition(self.requisition.pk, self.executive_director, "APPROVE")

        download_url = reverse("requisition-download", args=[self.requisition.pk])

        # Employees and non-finance reviewers cannot download the filing form
        self.client.force_login(self.employee)
        emp_detail = self.client.get(reverse("requisition-detail", args=[self.requisition.pk]))
        self.assertNotContains(emp_detail, download_url)
        emp_download = self.client.get(download_url)
        self.assertEqual(emp_download.status_code, 403)

        # Finance officer sees the download control in dashboard, approval queue, and detail
        self.client.force_login(self.finance)
        fin_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(fin_dashboard, download_url)

        fin_queue = self.client.get(reverse("approval-queue"))
        self.assertContains(fin_queue, download_url)

        fin_detail = self.client.get(reverse("requisition-detail", args=[self.requisition.pk]))
        self.assertContains(fin_detail, download_url)

        pdf_resp = self.client.get(download_url)
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
        self.assertIn(
            f'attachment; filename="requisition-{self.requisition.pk}.pdf"',
            pdf_resp["Content-Disposition"],
        )
        self.assertTrue(pdf_resp.content.startswith(b"%PDF-1.4"))
        self.assertIn(b"Community workshop", pdf_resp.content)
        self.assertIn(b"Program Lead", pdf_resp.content)
        self.assertNotIn(b"Program Director", pdf_resp.content)

    def test_saved_signature_image_is_stamped_on_pdf_when_approved(self):
        """A user's saved signature image is embedded into the PDF upon approval."""
        # Minimal valid 1x1 RGB PNG
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.client.force_login(self.executive_director)
                upload_resp = self.client.post(
                    reverse("signature-settings"),
                    {
                        "signature_image": SimpleUploadedFile(
                            "ed_sig.png", png_1x1, content_type="image/png"
                        )
                    },
                )
                self.assertEqual(upload_resp.status_code, 302)
                self.executive_director.refresh_from_db()
                self.assertTrue(bool(self.executive_director.signature_image))

                transition_requisition(self.requisition.pk, self.employee, "SUBMIT")
                transition_requisition(self.requisition.pk, self.supervisor, "APPROVE")
                transition_requisition(self.requisition.pk, self.me_officer, "APPROVE")
                transition_requisition(self.requisition.pk, self.program_lead, "APPROVE")
                transition_requisition(self.requisition.pk, self.executive_director, "APPROVE")

                self.client.force_login(self.finance)
                pdf_resp = self.client.get(
                    reverse("requisition-download", args=[self.requisition.pk])
                )
                self.assertEqual(pdf_resp.status_code, 200)
                self.assertIn(b"/SigApp Do", pdf_resp.content)
