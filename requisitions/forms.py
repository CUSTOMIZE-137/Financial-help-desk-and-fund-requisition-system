"""HTML forms for creating requests and submitting accountability evidence."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import forms
from django.core.exceptions import ValidationError

from requisitions.models import (
    AccountabilitySubmission,
    Requisition,
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
