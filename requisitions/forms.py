"""HTML forms for creating requests and submitting accountability evidence."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError

from requisitions.models import (
    AccountabilitySubmission,
    Requisition,
    User,
    validate_uploaded_evidence_file,
)


PROGRAM_ACTIVITY_FIELDS = (
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
)


class RequisitionForm(forms.ModelForm):
    """Validate activity-plan fields and calculate the budget from its line items."""

    class Meta:
        """Select user-editable fields and browser-friendly widgets."""

        model = Requisition
        fields = [
            "activity_type",
            "title",
            "description",
            "amount",
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
        ]
        labels = {
            "activity_type": "Activity",
            "title": "Activity Title",
            "activity_date": "Date of the Activity",
            "team_lead": "Team lead",
            "impact": "Impact",
            "program": "Program",
            "indicator": "Indicator",
            "activity_overview": "Overview of the Activity",
            "expected_outcomes": "Expected outcomes",
            "expected_outputs": "Expected outputs",
            "target_number": "Target Number",
            "inputs": "Inputs",
            "other_staff": "Other staff who will be involved in your activity",
            "amount": "Total budget",
            "risk_assessment": "Risk assessment plan and mitigation for the planned activity",
            "reporting_strategy": "Reporting strategy",
        }
        widgets = {
            "activity_type": forms.Select(attrs={"id": "id_activity_type", "class": "form-select"}),
            "amount": forms.HiddenInput(),
            "title": forms.TextInput(attrs={"placeholder": "e.g. NAKAWA OUTREACH"}),
            "description": forms.Textarea(attrs={"rows": 4, "placeholder": "Enter purpose and detailed justification"}),
            "activity_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "team_lead": forms.TextInput(attrs={"placeholder": "Full name of team lead"}),
            "program": forms.TextInput(attrs={"placeholder": "e.g. Child Care, Community Outreach, Therapy"}),
            "target_number": forms.TextInput(attrs={"placeholder": "e.g. 50 children"}),
            "impact": forms.Textarea(attrs={"rows": 3, "placeholder": "Describe the intended community or organizational impact"}),
            "indicator": forms.Textarea(attrs={"rows": 3, "placeholder": "Key performance or monitoring indicators"}),
            "activity_overview": forms.Textarea(attrs={"rows": 4, "placeholder": "Summary narrative of the planned activity"}),
            "expected_outcomes": forms.Textarea(attrs={"rows": 3, "placeholder": "Outcomes expected from this activity"}),
            "expected_outputs": forms.Textarea(attrs={"rows": 3, "placeholder": "Tangible outputs or deliverables"}),
            "inputs": forms.Textarea(attrs={"rows": 3, "placeholder": "Key resources and inputs required"}),
            "other_staff": forms.Textarea(attrs={"rows": 2, "placeholder": "Names / roles of other participating staff"}),
            "risk_assessment": forms.Textarea(attrs={"rows": 4, "placeholder": "Risk assessment plan and mitigation for the planned activity"}),
            "reporting_strategy": forms.Textarea(attrs={"rows": 3, "placeholder": "Reporting strategy and timelines"}),
        }

    def __init__(self, *args, **kwargs):
        """Configure required fields and the hidden, calculated request total."""
        super().__init__(*args, **kwargs)
        self.fields["title"].required = True
        self.fields["description"].required = True
        self.fields["amount"].required = False
        for field_name in PROGRAM_ACTIVITY_FIELDS:
            self.fields[field_name].required = False

    def clean(self):
        """Validate the selected activity and compute its budget from submitted rows."""
        cleaned_data = super().clean()
        activity_type = cleaned_data.get("activity_type")
        descriptions = self.data.getlist("item_description[]") if self.is_bound else []
        quantities = self.data.getlist("quantity[]") if self.is_bound else []
        unit_costs = self.data.getlist("unit_cost[]") if self.is_bound else []
        row_count = max(len(descriptions), len(quantities), len(unit_costs))
        total = Decimal("0.00")
        item_count = 0

        for index in range(row_count):
            description = descriptions[index].strip() if index < len(descriptions) else ""
            quantity = quantities[index].strip() if index < len(quantities) else ""
            unit_cost = unit_costs[index].strip() if index < len(unit_costs) else ""
            if not description and not quantity and not unit_cost:
                continue
            if not description:
                self.add_error(None, "Enter an item for every budget row with a quantity or cost.")
                continue
            try:
                quantity_value = Decimal(quantity)
                unit_cost_value = Decimal(unit_cost)
                if not quantity_value.is_finite() or quantity_value <= 0:
                    raise InvalidOperation
                if not unit_cost_value.is_finite() or unit_cost_value < 0:
                    raise InvalidOperation
                if len(quantity_value.as_tuple().digits) > 10 or quantity_value.as_tuple().exponent < -2:
                    raise InvalidOperation
                if len(unit_cost_value.as_tuple().digits) > 14 or unit_cost_value.as_tuple().exponent < -2:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                self.add_error(None, "Enter a valid positive quantity and non-negative unit cost for each budget item.")
                continue

            total += (quantity_value * unit_cost_value).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            item_count += 1

        if not item_count:
            self.add_error(None, "Add at least one item to the budget.")
        elif total <= 0:
            self.add_error(None, "The total budget must be greater than zero.")
        elif len(total.as_tuple().digits) > 14:
            self.add_error(None, "The total budget is too large.")
        else:
            cleaned_data["amount"] = total

        if activity_type == Requisition.ACTIVITY_NON_ADMINISTRATIVE:
            if not cleaned_data.get("indicator"):
                self.add_error("indicator", "An indicator is required for program activities.")
        elif activity_type == Requisition.ACTIVITY_ADMINISTRATIVE:
            for field_name in PROGRAM_ACTIVITY_FIELDS:
                cleaned_data[field_name] = None if field_name == "activity_date" else ""

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
        """Apply upload-size and file-signature validation before evidence is persisted."""
        cleaned_data = super().clean()
        for field_name in ("receipt_file", "invoice_file"):
            uploaded_file = cleaned_data.get(field_name)
            if uploaded_file:
                try:
                    validate_uploaded_evidence_file(uploaded_file)
                except ValidationError as exc:
                    for message in exc.messages:
                        self.add_error(field_name, message)
        return cleaned_data


class UserSignatureForm(forms.ModelForm):
    """Allow a user to upload or replace their saved e-signature image."""

    class Meta:
        model = User
        fields = ["signature_image"]
        labels = {"signature_image": "Signature image (PNG or JPEG)"}

    def clean_signature_image(self):
        uploaded_file = self.cleaned_data.get("signature_image")
        if uploaded_file:
            validate_uploaded_evidence_file(uploaded_file)
        return uploaded_file


class UserEnrollmentForm(forms.Form):
    """Administrator form for enrolling a new staff user with temporary credentials."""

    username = forms.CharField(
        max_length=150,
        label="Username",
        widget=forms.TextInput(attrs={"placeholder": "e.g. jdoe"}),
    )
    email = forms.EmailField(
        required=True,
        label="Email address",
        widget=forms.EmailInput(attrs={"placeholder": "e.g. jdoe@organization.org"}),
    )
    first_name = forms.CharField(
        max_length=150,
        required=False,
        label="First name",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Jane"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        label="Last name",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Doe"}),
    )
    role = forms.ChoiceField(
        choices=User.ROLE_CHOICES,
        initial=User.ROLE_EMPLOYEE,
        label="Workflow role",
    )
    department = forms.CharField(
        max_length=120,
        required=False,
        label="Department",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Programs, Finance, Operations"}),
    )
    send_invitation = forms.BooleanField(
        required=False,
        initial=True,
        label="Send email invitation with temporary credentials",
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username=username).exists():
            raise ValidationError(f"A user with username '{username}' already exists.")
        return username


class BulkUserEnrollmentForm(forms.Form):
    """Administrator form for enrolling multiple staff accounts from a CSV upload."""

    csv_file = forms.FileField(
        label="Staff CSV file",
        help_text="Columns: username, email, first_name, last_name, role, department",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    send_invitation = forms.BooleanField(
        required=False,
        initial=True,
        label="Send email invitations to enrolled users",
    )
    reset_existing = forms.BooleanField(
        required=False,
        initial=False,
        label="Update existing accounts and re-issue temporary passwords if username already exists",
    )


class FirstLoginPasswordChangeForm(PasswordChangeForm):
    """Enforce setting a distinct new password when signing in with temporary credentials."""

    def clean(self):
        cleaned_data = super().clean()
        old_password = cleaned_data.get("old_password")
        new_password1 = cleaned_data.get("new_password1")
        if old_password and new_password1 and old_password == new_password1:
            self.add_error(
                "new_password1",
                "Your new password must be different from your temporary password.",
            )
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.must_change_password = False
        if commit:
            user.save(update_fields=["password", "must_change_password"])
        return user

