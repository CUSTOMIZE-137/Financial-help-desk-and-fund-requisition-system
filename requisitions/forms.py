"""HTML forms for creating requests and submitting accountability evidence."""

from django import forms

from requisitions.models import AccountabilitySubmission, BudgetLine, Project, Requisition


class RequisitionForm(forms.ModelForm):
    """Validate draft request details and constrain budget choices by project."""

    class Meta:
        """Select user-editable fields and browser-friendly widgets."""

        model = Requisition
        fields = [
            "project",
            "budget_line",
            "title",
            "description",
            "amount",
            "currency",
            "due_date",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        """Offer active projects and, when selected, only their budget lines."""
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(is_active=True)
        self.fields["budget_line"].queryset = BudgetLine.objects.filter(
            project__is_active=True
        ).select_related("project")
        project_id = self.data.get("project") if self.is_bound else None
        if project_id and project_id.isdecimal():
            self.fields["budget_line"].queryset = self.fields["budget_line"].queryset.filter(
                project_id=project_id
            )

    def clean(self):
        """Require the selected budget line to belong to the selected project."""
        cleaned_data = super().clean()
        project = cleaned_data.get("project")
        budget_line = cleaned_data.get("budget_line")
        if project and budget_line and budget_line.project_id != project.pk:
            self.add_error("budget_line", "Choose a budget line from the selected project.")
        return cleaned_data


class AccountabilityForm(forms.ModelForm):
    """Collect liquidation details and bound uploaded file sizes."""

    class Meta:
        """Expose spend, report, and supporting evidence fields."""

        model = AccountabilitySubmission
        fields = [
            "actual_amount_spent",
            "activity_report",
            "receipt_reference",
            "invoice_reference",
            "receipt_file",
            "invoice_file",
        ]
        widgets = {
            "activity_report": forms.Textarea(attrs={"rows": 5}),
        }

    def clean(self):
        """Apply an upload-size limit before evidence is persisted."""
        cleaned_data = super().clean()
        for field_name in ("receipt_file", "invoice_file"):
            uploaded_file = cleaned_data.get(field_name)
            if uploaded_file and uploaded_file.size > 15 * 1024 * 1024:
                self.add_error(field_name, "Files must be 15 MB or smaller.")
        return cleaned_data
