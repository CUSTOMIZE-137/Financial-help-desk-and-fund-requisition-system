"""DRF serializers for authentication and requisition-related resources."""

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import FileExtensionValidator
from django.urls import reverse
from rest_framework import serializers

from requisitions.models import (
    AccountabilitySubmission,
    Attachment,
    Requisition,
    RequisitionItem,
    RequisitionLog,
    User,
)


class UserSerializer(serializers.ModelSerializer):
    """Serialize staff accounts while accepting passwords only on creation."""

    password = serializers.CharField(write_only=True, required=True, min_length=8)

    class Meta:
        """Expose account fields while keeping credentials write-only."""

        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "role",
            "department",
            "delegate",
            "delegate_start_date",
            "delegate_end_date",
        ]

    def validate_password(self, value):
        """Apply Django's configured password policy to API-created accounts."""
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def create(self, validated_data):
        """Hash the submitted password instead of storing it as plain text."""
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.full_clean()
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    """Authenticate API login credentials against Django's user backend."""

    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        """Attach the authenticated user or return a generic credential error."""
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError("Invalid credentials.")
        attrs["user"] = user
        return attrs




class RequisitionItemSerializer(serializers.ModelSerializer):
    """Serialize individual budget line items."""

    class Meta:
        model = RequisitionItem
        fields = ["id", "item_description", "quantity", "unit_cost", "total_cost"]


class RequisitionSerializer(serializers.ModelSerializer):
    """Expose request details while keeping ownership and workflow state read-only."""

    requestor = serializers.PrimaryKeyRelatedField(read_only=True)
    status = serializers.CharField(read_only=True)
    activity_display = serializers.CharField(source="get_activity_type_display", read_only=True)
    items = RequisitionItemSerializer(many=True, read_only=True)

    class Meta:
        """Keep ownership and workflow-controlled fields read-only."""

        model = Requisition
        fields = [
            "id",
            "requestor",
            "activity_type",
            "activity_display",
            "title",
            "description",
            "amount",
            "currency",
            "voucher_number",
            "disbursement_method",
            "status",
            "department",
            "requested_at",
            "due_date",
            "activity_date",
            "team_lead",
            "impact",
            "program",
            "indicator",
            "activity_overview",
            "expected_outcomes",
            "expected_outputs",
            "target_number",
            "inputs",
            "other_staff",
            "risk_assessment",
            "reporting_strategy",
            "items",
            "last_comment",
        ]
        read_only_fields = [
            "id",
            "requestor",
            "status",
            "department",
            "requested_at",
            "voucher_number",
            "disbursement_method",
        ]

    def validate(self, attrs):
        """Check model constraints against the instance plus incoming values."""
        instance = self.instance or Requisition(requestor=self.context["request"].user)
        for field, value in attrs.items():
            setattr(instance, field, value)
        try:
            instance.full_clean(exclude=["status", "requested_at", "updated_at"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class RequisitionLogSerializer(serializers.ModelSerializer):
    """Return audit entries as read-only history, including actor display names."""

    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True)

    class Meta:
        """Make audit history entirely read-only to API clients."""

        model = RequisitionLog
        fields = [
            "id",
            "requisition",
            "actor",
            "actor_name",
            "action",
            "previous_status",
            "new_status",
            "comments",
            "timestamp",
        ]
        read_only_fields = fields


class AccountabilitySubmissionSerializer(serializers.ModelSerializer):
    """Validate liquidation evidence and return protected file-download links."""

    # File storage paths are not returned; clients receive authenticated URLs instead.
    submitted_by = serializers.PrimaryKeyRelatedField(read_only=True)
    receipt_file = serializers.FileField(
        required=False, allow_empty_file=False, write_only=True
    )
    invoice_file = serializers.FileField(
        required=False, allow_empty_file=False, write_only=True
    )
    receipt_download_url = serializers.SerializerMethodField()
    invoice_download_url = serializers.SerializerMethodField()

    class Meta:
        """Hide file storage fields from output and protect workflow metadata."""

        model = AccountabilitySubmission
        fields = [
            "id",
            "requisition",
            "submitted_by",
            "actual_amount_spent",
            "activity_report",
            "receipt_reference",
            "invoice_reference",
            "receipt_file",
            "invoice_file",
            "receipt_download_url",
            "invoice_download_url",
            "unspent_amount",
            "submitted_at",
            "finance_approved_at",
            "audit_approved_at",
        ]
        read_only_fields = [
            "id",
            "receipt_download_url",
            "invoice_download_url",
            "unspent_amount",
            "submitted_by",
            "submitted_at",
            "finance_approved_at",
            "audit_approved_at",
        ]

    def validate(self, attrs):
        """Allow submissions only for disbursed requests and validate their amounts."""
        requisition = attrs.get("requisition")
        if requisition is None or requisition.status != Requisition.STATUS_DISBURSED:
            raise serializers.ValidationError(
                {"requisition": "Accountability can only be submitted for disbursed requisitions."}
            )
        instance = AccountabilitySubmission(
            requisition=requisition,
            submitted_by=self.context["request"].user,
            **{key: value for key, value in attrs.items() if key != "requisition"},
        )
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs

    def validate_receipt_file(self, file):
        """Reject receipt uploads over the configured per-file size limit."""
        if file.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("Files must be 15 MB or smaller.")
        return file

    def validate_invoice_file(self, file):
        """Reject invoice uploads over the configured per-file size limit."""
        if file.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("Files must be 15 MB or smaller.")
        return file

    def get_receipt_download_url(self, obj):
        """Build a protected download URL when a receipt has been uploaded."""
        if not obj.receipt_file:
            return None
        return self._protected_download_url("receipt", obj.pk)

    def get_invoice_download_url(self, obj):
        """Build a protected download URL when an invoice has been uploaded."""
        if not obj.invoice_file:
            return None
        return self._protected_download_url("invoice", obj.pk)

    def _protected_download_url(self, kind, pk):
        """Build an absolute URL when request context is available."""
        path = reverse("api-protected-file", kwargs={"kind": kind, "pk": pk})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class AttachmentSerializer(serializers.ModelSerializer):
    """Accept new attachment files and expose only protected download URLs."""

    uploaded_by = serializers.PrimaryKeyRelatedField(read_only=True)
    file = serializers.FileField(
        write_only=True,
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )
    download_url = serializers.SerializerMethodField()

    class Meta:
        """Expose attachment metadata and a protected download URL."""

        model = Attachment
        fields = [
            "id",
            "requisition",
            "file",
            "download_url",
            "file_type",
            "uploaded_by",
            "uploaded_at",
        ]
        read_only_fields = ["id", "download_url", "uploaded_by", "uploaded_at"]

    def get_download_url(self, obj):
        """Build a URL whose view enforces request visibility before download."""
        path = reverse("api-protected-file", kwargs={"kind": "attachment", "pk": obj.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def validate_file(self, file):
        """Reject attachment uploads over the configured per-file size limit."""
        if file.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("Files must be 15 MB or smaller.")
        return file


class RequisitionActionSerializer(serializers.Serializer):
    """Validate the action and optional comment sent to a transition endpoint."""

    action = serializers.ChoiceField(choices=RequisitionLog.ACTION_CHOICES)
    comments = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    disbursement_method = serializers.ChoiceField(
        choices=Requisition.DISBURSEMENT_METHOD_CHOICES,
        required=False,
        allow_blank=True,
    )

