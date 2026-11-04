"""Django admin configuration for finance setup and audit review."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from requisitions.models import (
    AccountabilitySubmission,
    Attachment,
    Requisition,
    RequisitionItem,
    RequisitionLog,
    User,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Include workflow roles and delegation settings on user admin pages."""

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Help desk access",
            {
                "fields": (
                    "role",
                    "department",
                    "signature_image",
                    "delegate",
                    "delegate_start_date",
                    "delegate_end_date",
                )
            },
        ),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Help desk access",
            {
                "fields": (
                    "role",
                    "department",
                    "signature_image",
                )
            },
        ),
    )
    list_display = ("username", "email", "role", "department", "is_active")
    list_filter = ("role", "department", "is_active")


class RequisitionItemInline(admin.TabularInline):
    """Display itemized budget breakdown on a requisition."""

    model = RequisitionItem
    extra = 1


class RequisitionLogInline(admin.TabularInline):
    """Display audit history on a request without allowing edits or new entries."""

    model = RequisitionLog
    extra = 0
    can_delete = False
    readonly_fields = [field.name for field in RequisitionLog._meta.fields]

    def has_add_permission(self, request, obj=None):
        """Only workflow actions, not admin forms, may add audit records."""
        return False


@admin.register(Requisition)
class RequisitionAdmin(admin.ModelAdmin):
    """Show requests while protecting workflow state from direct admin edits."""

    list_display = (
        "title",
        "activity_type",
        "requestor",
        "amount",
        "currency",
        "voucher_number",
        "disbursement_method",
        "status",
        "requested_at",
    )
    list_filter = ("status", "activity_type", "department")
    search_fields = ("title", "requestor__username", "team_lead", "program")
    readonly_fields = (
        "status",
        "requested_at",
        "updated_at",
        "approved_at",
        "disbursed_at",
        "voucher_number",
        "disbursement_method",
    )
    inlines = [RequisitionItemInline, RequisitionLogInline]

    def get_readonly_fields(self, request, obj=None):
        """Freeze all request fields once approval or processing has started."""
        if obj and obj.status not in (
            Requisition.STATUS_DRAFT,
            Requisition.STATUS_REVISION_REQUESTED,
        ):
            return [field.name for field in self.model._meta.fields]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        """Allow deletion only while a request is still a draft."""
        return bool(obj and obj.status == Requisition.STATUS_DRAFT)


@admin.register(AccountabilitySubmission)
class AccountabilitySubmissionAdmin(admin.ModelAdmin):
    """Present liquidation records as read-only finance review data."""

    list_display = (
        "requisition",
        "submitted_by",
        "actual_amount_spent",
            "unspent_amount",
        "submitted_at",
        "audit_approved_at",
    )
    readonly_fields = [field.name for field in AccountabilitySubmission._meta.fields]

    def has_add_permission(self, request):
        """Require submissions to pass through the validated workflow."""
        return False

    def has_change_permission(self, request, obj=None):
        """Prevent manual changes to submitted accountability evidence."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Preserve accountability records after submission."""
        return False


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """Show attachment metadata without exposing mutation controls."""

    list_display = ("requisition", "file_type", "uploaded_by", "uploaded_at")
    readonly_fields = [field.name for field in Attachment._meta.fields]

    def has_add_permission(self, request):
        """Require uploads to pass through the validated application flow."""
        return False

    def has_change_permission(self, request, obj=None):
        """Keep uploaded attachment records read-only."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Preserve supporting evidence after it has been uploaded."""
        return False
