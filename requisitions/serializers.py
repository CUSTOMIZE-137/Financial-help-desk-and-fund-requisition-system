"""DRF serializers for authentication and requisition-related resources."""

from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.urls import reverse
from rest_framework import serializers

from requisitions.enrollment import generate_temporary_password, send_enrollment_invitation
from requisitions.models import (
    AccountabilitySubmission,
    Attachment,
    Requisition,
    RequisitionItem,
    RequisitionLog,
    User,
    validate_uploaded_evidence_file,
)
from requisitions.workflow import can_download_requisition_form


class UserSerializer(serializers.ModelSerializer):
    """Serialize staff accounts while supporting temporary credential generation and invitations."""

    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, min_length=8
    )
    send_invitation = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    temporary_password = serializers.CharField(read_only=True)

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
            "temporary_password",
            "must_change_password",
            "send_invitation",
            "role",
            "department",
            "delegate",
            "delegate_start_date",
            "delegate_end_date",
        ]

    def validate_password(self, value):
        """Apply Django's configured password policy to API-created accounts."""
        if not value:
            return value
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def create(self, validated_data):
        """Hash the submitted or generated password and optionally send an enrollment email."""
        password = validated_data.pop("password", "")
        send_invitation = validated_data.pop("send_invitation", False)
        explicit_must_change = "must_change_password" in validated_data
        user = User(**validated_data)

        generated_temp = False
        if not password:
            password = generate_temporary_password(user=user)
            generated_temp = True
            if not explicit_must_change:
                user.must_change_password = True

        user.set_password(password)
        user.full_clean()
        user.save()

        if generated_temp or user.must_change_password:
            user.temporary_password = password
        if send_invitation and user.email:
            send_enrollment_invitation(
                user, password, request=self.context.get("request")
            )
        return user


class PasswordChangeSerializer(serializers.Serializer):
    """Validate and apply a password change for an authenticated user."""

    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Your current password was entered incorrectly.")
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        old_password = attrs.get("old_password")
        new_password = attrs.get("new_password")
        if old_password == new_password:
            raise serializers.ValidationError(
                {"new_password": "Your new password must be different from your temporary password."}
            )
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)}) from exc
        return attrs


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

    quantity = serializers.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    unit_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    total_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = RequisitionItem
        fields = ["id", "item_description", "quantity", "unit_cost", "total_cost"]
        read_only_fields = ["id", "total_cost"]


class RequisitionSerializer(serializers.ModelSerializer):
    """Expose request details while keeping ownership and workflow state read-only."""

    requestor = serializers.PrimaryKeyRelatedField(read_only=True)
    status = serializers.CharField(read_only=True)
    activity_display = serializers.CharField(source="get_activity_type_display", read_only=True)
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    items = RequisitionItemSerializer(many=True, required=False)
    form_download_url = serializers.SerializerMethodField()

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
            "form_download_url",
        ]
        read_only_fields = [
            "id",
            "requestor",
            "status",
            "department",
            "requested_at",
            "voucher_number",
            "disbursement_method",
            "form_download_url",
        ]

    def get_form_download_url(self, obj):
        """Return a PDF form download link when the current user is in Finance or Admin."""
        request = self.context.get("request")
        if not request or not can_download_requisition_form(request.user, obj):
            return None
        path = reverse("api-requisition-download", kwargs={"pk": obj.pk})
        return request.build_absolute_uri(path)

    def validate(self, attrs):
        """Check model constraints against the instance plus incoming values."""
        items_data = attrs.get("items")
        if items_data is not None:
            if not items_data:
                raise serializers.ValidationError(
                    {"items": "Add at least one item to the budget."}
                )
            computed_total = sum(
                (
                    (item["quantity"] * item["unit_cost"]).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    for item in items_data
                ),
                Decimal("0.00"),
            )
            if computed_total <= Decimal("0.00"):
                raise serializers.ValidationError(
                    {"amount": "The total budget must be greater than zero."}
                )
            attrs["amount"] = computed_total
        elif self.instance is None and "amount" not in attrs:
            raise serializers.ValidationError(
                {"amount": "Provide a budget amount or at least one budget line item."}
            )

        instance = self.instance or Requisition(requestor=self.context["request"].user)
        for field, value in attrs.items():
            if field == "items":
                continue
            setattr(instance, field, value)
        try:
            instance.full_clean(exclude=["status", "requested_at", "updated_at"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs

    def _save_items(self, requisition, items_data):
        """Replace line items in bulk and recalculate each row's total cost."""
        requisition.items.all().delete()
        rows = []
        for item in items_data:
            qty = item["quantity"]
            unit_cost = item["unit_cost"]
            total_cost = (qty * unit_cost).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            rows.append(
                RequisitionItem(
                    requisition=requisition,
                    item_description=item["item_description"].strip(),
                    quantity=qty,
                    unit_cost=unit_cost,
                    total_cost=total_cost,
                )
            )
        if rows:
            RequisitionItem.objects.bulk_create(rows)

    def create(self, validated_data):
        """Create the requisition and any supplied budget line items."""
        items_data = validated_data.pop("items", None)
        requisition = super().create(validated_data)
        if items_data is not None:
            self._save_items(requisition, items_data)
        return requisition

    def update(self, instance, validated_data):
        """Update the requisition and replace budget line items if supplied."""
        items_data = validated_data.pop("items", None)
        requisition = super().update(instance, validated_data)
        if items_data is not None:
            self._save_items(requisition, items_data)
        return requisition


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
        """Reject receipt uploads that exceed the size limit or fail content signature checks."""
        try:
            validate_uploaded_evidence_file(file)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return file

    def validate_invoice_file(self, file):
        """Reject invoice uploads that exceed the size limit or fail content signature checks."""
        try:
            validate_uploaded_evidence_file(file)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
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
        """Reject attachment uploads that exceed the size limit or fail content signature checks."""
        try:
            validate_uploaded_evidence_file(file)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
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

