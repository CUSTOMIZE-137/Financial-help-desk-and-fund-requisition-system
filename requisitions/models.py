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
    ROLE_PROGRAM_LEAD = "PROGRAM_LEAD"
    ROLE_PROGRAM_DIRECTOR = ROLE_PROGRAM_LEAD
    ROLE_EXECUTIVE_DIRECTOR = "EXECUTIVE_DIRECTOR"
    ROLE_FINANCE = "FINANCE"
    ROLE_INTERNAL_AUDIT = "INTERNAL_AUDIT"
    ROLE_ADMIN = "ADMIN"
    ROLE_CHOICES = [
        (ROLE_EMPLOYEE, "Employee"),
        (ROLE_SUPERVISOR, "Supervisor"),
        (ROLE_ME_OFFICER, "M&E Officer"),
        (ROLE_PROGRAM_LEAD, "Program Lead"),
        (ROLE_EXECUTIVE_DIRECTOR, "Executive Director"),
        (ROLE_FINANCE, "Finance"),
        (ROLE_INTERNAL_AUDIT, "Internal Audit"),
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
    signature_image = models.FileField(
        upload_to="signatures/%Y/%m/%d/",
        blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg"])],
        verbose_name="Signature Image",
        help_text="Upload a PNG or JPEG image of your signature to be stamped on approved requisitions.",
    )
    must_change_password = models.BooleanField(
        default=False,
        verbose_name="Require password change on next sign-in",
        help_text="When enabled, the user must set a new password upon signing in before accessing the workspace.",
    )

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
        """Reject self-delegation, invalid date ranges, and disguised signature files."""
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
        if self.signature_image:
            try:
                validate_uploaded_evidence_file(self.signature_image)
            except ValidationError as exc:
                raise ValidationError({"signature_image": exc.messages}) from exc

    def __str__(self):
        """Show the user's name and assigned workflow role in admin lists."""
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    class Meta(AbstractUser.Meta):
        indexes = [
            models.Index(fields=["role", "department"], name="user_role_dept_idx"),
            models.Index(
                fields=["delegate", "is_active", "delegate_start_date", "delegate_end_date"],
                name="user_active_delegate_idx",
            ),
        ]


class Requisition(models.Model):
    """A fund request whose status advances through the approval workflow."""

    # Keep these persisted values aligned with app.workflow.STATUS_FLOW.
    STATUS_DRAFT = "DRAFT"
    STATUS_PENDING_SUPERVISOR = "PENDING_SUPERVISOR"
    STATUS_PENDING_ME = "PENDING_ME"
    STATUS_PENDING_PROGRAM_LEAD = "PENDING_PROGRAM_LEAD"
    STATUS_PENDING_EXECUTIVE_DIRECTOR = "PENDING_EXECUTIVE_DIRECTOR"
    STATUS_PENDING_DIRECTOR = STATUS_PENDING_EXECUTIVE_DIRECTOR
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
        (STATUS_PENDING_PROGRAM_LEAD, "Pending Program Lead"),
        (STATUS_PENDING_EXECUTIVE_DIRECTOR, "Pending Executive Director"),
        (STATUS_PENDING_FINANCE, "Pending Finance"),
        (STATUS_DISBURSED, "Disbursed"),
        (STATUS_AWAITING_ACCOUNTABILITY, "Awaiting Internal Audit"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_REVISION_REQUESTED, "Revision Requested"),
    ]

    ACTIVITY_NON_ADMINISTRATIVE = "NON_ADMINISTRATIVE"
    ACTIVITY_PROGRAMS = ACTIVITY_NON_ADMINISTRATIVE
    ACTIVITY_ADMINISTRATIVE = "ADMINISTRATIVE"
    ACTIVITY_CHOICES = [
        (ACTIVITY_NON_ADMINISTRATIVE, "Programs"),
        (ACTIVITY_ADMINISTRATIVE, "Administrative Activity"),
    ]
    DISBURSEMENT_BANK_TRANSFER = "BANK_TRANSFER"
    DISBURSEMENT_PETTY_CASH = "PETTY_CASH"
    DISBURSEMENT_METHOD_CHOICES = [
        (DISBURSEMENT_BANK_TRANSFER, "Bank transfer"),
        (DISBURSEMENT_PETTY_CASH, "Petty cash"),
    ]

    requestor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requisitions",
    )
    activity_type = models.CharField(
        max_length=32,
        choices=ACTIVITY_CHOICES,
        default=ACTIVITY_NON_ADMINISTRATIVE,
        verbose_name="Activity",
    )
    title = models.CharField(max_length=200, verbose_name="Activity Title")
    description = models.TextField(blank=True, verbose_name="Description / Purpose")
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name="Budget Amount"
    )
    currency = models.CharField(max_length=3, default="UGX")
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )
    department = models.CharField(max_length=120, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    disbursed_at = models.DateTimeField(null=True, blank=True)
    voucher_number = models.CharField(max_length=40, blank=True)
    disbursement_method = models.CharField(
        max_length=32, choices=DISBURSEMENT_METHOD_CHOICES, blank=True
    )
    due_date = models.DateField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    last_comment = models.TextField(blank=True)

    # Mukisa Foundation Activity Plan template fields (for Non-administrative Activities)
    activity_date = models.DateField(null=True, blank=True, verbose_name="Date of the Activity")
    team_lead = models.CharField(max_length=200, blank=True, verbose_name="Team lead")
    impact = models.TextField(blank=True, verbose_name="Impact")
    program = models.CharField(max_length=200, blank=True, verbose_name="Program")
    indicator = models.TextField(blank=True, verbose_name="Indicator")
    activity_overview = models.TextField(blank=True, verbose_name="Overview of the Activity")
    expected_outcomes = models.TextField(blank=True, verbose_name="Expected outcomes")
    expected_outputs = models.TextField(blank=True, verbose_name="Expected outputs")
    target_number = models.CharField(max_length=120, blank=True, verbose_name="Target Number")
    inputs = models.TextField(blank=True, verbose_name="Inputs")
    other_staff = models.TextField(
        blank=True, verbose_name="Other staff who will be involved in your activity"
    )
    risk_assessment = models.TextField(
        blank=True,
        verbose_name="Risk assessment plan and mitigation for the planned activity",
    )
    reporting_strategy = models.TextField(blank=True, verbose_name="Reporting strategy")

    @property
    def activity(self):
        """Alias property matching user terminology."""
        return self.activity_type

    @activity.setter
    def activity(self, value):
        self.activity_type = value

    @property
    def is_administrative(self):
        """Return whether this request is an administrative activity."""
        return self.activity_type == self.ACTIVITY_ADMINISTRATIVE

    class Meta:
        """Order requests by recency and declare workflow-specific permissions."""

        ordering = ["-requested_at"]
        permissions = [
            ("review_requisition", "Can review a requisition"),
            ("disburse_requisition", "Can disburse a requisition"),
        ]
        indexes = [
            models.Index(
                fields=["status", "department", "activity_type"],
                name="req_status_dept_act_idx",
            ),
            models.Index(
                fields=["requestor", "-requested_at"],
                name="req_requestor_date_idx",
            ),
            models.Index(
                fields=["requested_at", "status"],
                name="req_reqdate_status_idx",
            ),
        ]

    def clean(self):
        """Validate dates, currency, and program-activity requirements."""
        super().clean()
        if self.due_date and self.due_date < date.today():
            raise ValidationError({"due_date": "The due date cannot be in the past."})
        if self.activity_type == self.ACTIVITY_NON_ADMINISTRATIVE and not self.indicator:
            raise ValidationError(
                {"indicator": "An indicator is required for program activities."}
            )
        if self.activity_type == self.ACTIVITY_NON_ADMINISTRATIVE and not self.description:
            if self.activity_overview:
                self.description = self.activity_overview

    def __str__(self):
        """Include the current state when displaying the request."""
        return f"{self.title} ({self.get_status_display()})"


class RequisitionItem(models.Model):
    """An itemized line item within a requisition budget."""

    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name="items"
    )
    item_description = models.CharField(max_length=255, verbose_name="Item")
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("1.00"), verbose_name="Quantity"
    )
    unit_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"), verbose_name="Unit Cost"
    )
    total_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"), verbose_name="Total Cost"
    )

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if not self.total_cost:
            self.total_cost = (self.quantity or Decimal("0")) * (self.unit_cost or Decimal("0"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_description} ({self.total_cost})"


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
        (ACTION_FINALIZE_ACCOUNTABILITY, "Validate and Close"),
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
        indexes = [
            models.Index(fields=["requisition", "-timestamp"], name="reqlog_req_time_idx"),
            models.Index(fields=["actor", "requisition"], name="reqlog_actor_req_idx"),
        ]

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


MAX_UPLOAD_SIZE_BYTES = 15 * 1024 * 1024
FILE_SIGNATURES = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
}


def validate_uploaded_evidence_file(uploaded_file):
    """Validate file size and magic-byte header for supported evidence types."""
    if not uploaded_file:
        return
    # Respect the runtime upload cap (4 MB on Vercel, 15 MB default) for serverless payload limits.
    max_upload_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 15)
    if uploaded_file.size > max_upload_mb * 1024 * 1024:
        raise ValidationError(f"Files must be {max_upload_mb} MB or smaller.")
    name = getattr(uploaded_file, "name", "") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    expected_prefixes = FILE_SIGNATURES.get(ext)
    if expected_prefixes:
        try:
            pos = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
            header = uploaded_file.read(8)
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(pos)
        except OSError:
            return
        if not any(header.startswith(prefix) for prefix in expected_prefixes):
            raise ValidationError(
                "Uploaded file content does not match its declared file extension."
            )


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
    audit_approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Show the most recently submitted accountability report first."""

        ordering = ["-submitted_at"]

    @property
    def unspent_amount(self):
        """Return the unspent portion of the approved amount for reconciliation."""
        return self.requisition.amount - self.actual_amount_spent

    def clean(self):
        """Ensure actual spend and uploaded evidence meet request limits."""
        super().clean()
        if self.requisition_id and self.actual_amount_spent > self.requisition.amount:
            raise ValidationError(
                {"actual_amount_spent": "Actual spending cannot exceed the approved amount."}
            )
        if self.actual_amount_spent and not any(
            (self.receipt_file, self.invoice_file, self.receipt_reference, self.invoice_reference)
        ):
            raise ValidationError(
                "Provide at least one receipt or invoice file or reference for reported spending."
            )
        for field_name in ("receipt_file", "invoice_file"):
            uploaded_file = getattr(self, field_name)
            if uploaded_file:
                try:
                    validate_uploaded_evidence_file(uploaded_file)
                except ValidationError as exc:
                    raise ValidationError({field_name: exc.messages}) from exc

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
        """Enforce the per-file upload size limit and content signature."""
        super().clean()
        if self.file:
            try:
                validate_uploaded_evidence_file(self.file)
            except ValidationError as exc:
                raise ValidationError({"file": exc.messages}) from exc

    def __str__(self):
        """Describe the attachment by type and associated request."""
        return f"{self.get_file_type_display()} for requisition {self.requisition_id}"


class BackgroundTask(models.Model):
    """Durable background job record for asynchronous emails, notifications, and PDF warming."""

    STATUS_PENDING = "PENDING"
    STATUS_RUNNING = "RUNNING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    task_name = models.CharField(max_length=120, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    last_error = models.TextField(blank=True)
    scheduled_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="bgtask_status_sched_idx",
            ),
        ]

    def __str__(self):
        return f"{self.task_name} ({self.status})"

