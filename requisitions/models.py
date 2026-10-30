"""Database models for users, budgets, requests, and supporting evidence."""

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Authenticated user with a workflow role and optional date-bound delegate."""

    # Role labels are used by both the approval state machine and access checks.
    ROLE_EMPLOYEE = "EMPLOYEE"
    ROLE_SUPERVISOR = "SUPERVISOR"
    ROLE_ME_OFFICER = "ME_OFFICER"
    ROLE_PROGRAM_DIRECTOR = "PROGRAM_DIRECTOR"
    ROLE_FINANCE = "FINANCE"
    ROLE_ADMIN = "ADMIN"
    ROLE_CHOICES = [
        (ROLE_EMPLOYEE, "Employee"),
        (ROLE_SUPERVISOR, "Supervisor"),
        (ROLE_ME_OFFICER, "M&E Officer"),
        (ROLE_PROGRAM_DIRECTOR, "Program Director"),
        (ROLE_FINANCE, "Finance"),
        (ROLE_ADMIN, "Admin"),
    ]

    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default=ROLE_EMPLOYEE)
    department = models.CharField(max_length=120, blank=True)
    delegate = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegated_users",
    )
    delegate_start_date = models.DateField(null=True, blank=True)
    delegate_end_date = models.DateField(null=True, blank=True)

    @property
    def has_active_delegate(self) -> bool:
        """Return whether the configured delegate is active today."""
        today = timezone.localdate()
        return bool(
            self.delegate_id
            and self.delegate_start_date
            and self.delegate_end_date
            and self.delegate_start_date <= today <= self.delegate_end_date
            and self.delegate.is_active
        )

    def clean(self):
        """Reject self-delegation and incomplete or inverted date ranges."""
        super().clean()
        if self.delegate_id == self.pk and self.pk:
            raise ValidationError({"delegate": "A user cannot delegate to themselves."})
        if bool(self.delegate_start_date) != bool(self.delegate_end_date):
            raise ValidationError(
                "Both delegation start and end dates must be provided together."
            )
        if (
            self.delegate_start_date
            and self.delegate_end_date
            and self.delegate_start_date > self.delegate_end_date
        ):
            raise ValidationError(
                {"delegate_end_date": "The end date must be on or after the start date."}
            )

    def __str__(self):
        """Show the user's name and assigned workflow role in admin lists."""
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


class Project(models.Model):
    """A funded project that groups budget lines and requisitions."""

    code = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        """Require a project end date to follow its optional start date."""
        super().clean()
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"end_date": "The end date must not precede the start date."})

    def __str__(self):
        """Return the project's human-readable identifier."""
        return f"{self.code} - {self.name}"


class BudgetLine(models.Model):
    """A project allocation and the amount already committed or spent."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="budget_lines"
    )
    code = models.CharField(max_length=80)
    category = models.CharField(max_length=120)
    allocated_amount = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(0)]
    )
    spent_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        """Keep budget lines unique within a project and amounts nonnegative."""

        ordering = ["project__code", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code"], name="unique_budget_line_per_project"
            ),
            models.CheckConstraint(
                check=models.Q(allocated_amount__gte=0),
                name="budget_allocated_nonnegative",
            ),
            models.CheckConstraint(
                check=models.Q(spent_amount__gte=0),
                name="budget_spent_nonnegative",
            ),
        ]

    @property
    def available_amount(self):
        """Compute the remaining allocation without storing duplicate state."""
        return self.allocated_amount - self.spent_amount

    def __str__(self):
        """Return a project-scoped budget-line identifier."""
        return f"{self.project.code}:{self.code}"


class Requisition(models.Model):
    """A fund request whose status advances through the approval workflow."""

    # Keep these persisted values aligned with app.workflow.STATUS_FLOW.
    STATUS_DRAFT = "DRAFT"
    STATUS_PENDING_SUPERVISOR = "PENDING_SUPERVISOR"
    STATUS_PENDING_ME = "PENDING_ME"
    STATUS_PENDING_DIRECTOR = "PENDING_DIRECTOR"
    STATUS_PENDING_FINANCE = "PENDING_FINANCE"
    STATUS_DISBURSED = "DISBURSED"
    STATUS_AWAITING_ACCOUNTABILITY = "AWAITING_ACCOUNTABILITY"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_REJECTED = "REJECTED"
    STATUS_REVISION_REQUESTED = "REVISION_REQUESTED"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING_SUPERVISOR, "Pending Supervisor"),
        (STATUS_PENDING_ME, "Pending M&E"),
        (STATUS_PENDING_DIRECTOR, "Pending Director"),
        (STATUS_PENDING_FINANCE, "Pending Finance"),
        (STATUS_DISBURSED, "Disbursed"),
        (STATUS_AWAITING_ACCOUNTABILITY, "Awaiting Accountability"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_REVISION_REQUESTED, "Revision Requested"),
    ]

    requestor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requisitions",
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="requisitions"
    )
    budget_line = models.ForeignKey(
        BudgetLine, on_delete=models.PROTECT, related_name="requisitions"
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )
    department = models.CharField(max_length=120, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    disbursed_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    last_comment = models.TextField(blank=True)

    class Meta:
        """Order requests by recency and declare workflow-specific permissions."""

        ordering = ["-requested_at"]
        permissions = [
            ("review_requisition", "Can review a requisition"),
            ("disburse_requisition", "Can disburse a requisition"),
        ]

    def clean(self):
        """Validate project/budget consistency, currency, and request dates."""
        super().clean()
        if self.budget_line_id and self.project_id:
            if self.budget_line.project_id != self.project_id:
                raise ValidationError(
                    {"budget_line": "Choose a budget line from the selected project."}
                )
            if self.budget_line.currency != self.currency:
                raise ValidationError(
                    {"currency": "Currency must match the selected budget line."}
                )
        if self.due_date and self.due_date < date.today():
            raise ValidationError({"due_date": "The due date cannot be in the past."})

    def __str__(self):
        """Include the current state when displaying the request."""
        return f"{self.title} ({self.get_status_display()})"


class RequisitionLog(models.Model):
    """Immutable record of an actor's action and its status transition."""

    ACTION_SUBMIT = "SUBMIT"
    ACTION_APPROVE = "APPROVE"
    ACTION_REQUEST_REVISION = "REQUEST_REVISION"
    ACTION_REJECT = "REJECT"
    ACTION_RESUBMIT = "RESUBMIT"
    ACTION_SUBMIT_ACCOUNTABILITY = "SUBMIT_ACCOUNTABILITY"
    ACTION_FINALIZE_ACCOUNTABILITY = "FINALIZE_ACCOUNTABILITY"
    ACTION_CHOICES = [
        (ACTION_SUBMIT, "Submit"),
        (ACTION_APPROVE, "Approve"),
        (ACTION_REQUEST_REVISION, "Request Revision"),
        (ACTION_REJECT, "Reject"),
        (ACTION_RESUBMIT, "Resubmit"),
        (ACTION_SUBMIT_ACCOUNTABILITY, "Submit Accountability"),
        (ACTION_FINALIZE_ACCOUNTABILITY, "Finalize Accountability"),
    ]

    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name="logs"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requisition_actions"
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    previous_status = models.CharField(max_length=32, choices=Requisition.STATUS_CHOICES)
    new_status = models.CharField(max_length=32, choices=Requisition.STATUS_CHOICES)
    comments = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        """Present the newest audit events first."""

        ordering = ["-timestamp"]

    def save(self, *args, **kwargs):
        """Allow initial creation but reject updates to existing audit entries."""
        if self.pk and RequisitionLog.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit log entries are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent deleting an audit record through the model instance."""
        raise ValidationError("Audit log entries cannot be deleted.")

    def __str__(self):
        """Identify the request and action represented by this log entry."""
        return f"{self.requisition_id}: {self.action}"


class AccountabilitySubmission(models.Model):
    """Liquidation report and supporting proof submitted after disbursement."""

    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name="accountability_submissions"
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accountability_submissions",
    )
    actual_amount_spent = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(0)]
    )
    activity_report = models.TextField()
    receipt_reference = models.CharField(max_length=255, blank=True)
    invoice_reference = models.CharField(max_length=255, blank=True)
    receipt_file = models.FileField(
        upload_to="receipts/%Y/%m/%d/",
        blank=True,
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    invoice_file = models.FileField(
        upload_to="invoices/%Y/%m/%d/",
        blank=True,
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    finance_approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Show the most recently submitted accountability report first."""

        ordering = ["-submitted_at"]

    def clean(self):
        """Ensure actual spend and uploaded evidence meet request limits."""
        super().clean()
        if self.requisition_id and self.actual_amount_spent > self.requisition.amount:
            raise ValidationError(
                {"actual_amount_spent": "Actual spending cannot exceed the approved amount."}
            )
        for field_name in ("receipt_file", "invoice_file"):
            uploaded_file = getattr(self, field_name)
            if uploaded_file and uploaded_file.size > 15 * 1024 * 1024:
                raise ValidationError({field_name: "Files must be 15 MB or smaller."})

    def __str__(self):
        """Identify the request covered by this accountability report."""
        return f"Accountability for requisition {self.requisition_id}"


class Attachment(models.Model):
    """A typed file associated with a requisition and its uploader."""

    ATTACHMENT_RECEIPT = "RECEIPT"
    ATTACHMENT_INVOICE = "INVOICE"
    ATTACHMENT_REPORT = "REPORT"
    ATTACHMENT_CHOICES = [
        (ATTACHMENT_RECEIPT, "Receipt"),
        (ATTACHMENT_INVOICE, "Invoice"),
        (ATTACHMENT_REPORT, "Activity Report"),
    ]

    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(
        upload_to="attachments/%Y/%m/%d/",
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    file_type = models.CharField(max_length=16, choices=ATTACHMENT_CHOICES)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_attachments"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        """Enforce the per-file upload size limit."""
        super().clean()
        if self.file and self.file.size > 15 * 1024 * 1024:
            raise ValidationError({"file": "Files must be 15 MB or smaller."})

    def __str__(self):
        """Describe the attachment by type and associated request."""
        return f"{self.get_file_type_display()} for requisition {self.requisition_id}"
