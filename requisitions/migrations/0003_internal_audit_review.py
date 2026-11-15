from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0002_requisition_activity_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="requisition",
            name="voucher_number",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="requisition",
            name="disbursement_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("BANK_TRANSFER", "Bank transfer"),
                    ("PETTY_CASH", "Petty cash"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="accountabilitysubmission",
            name="audit_approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("EMPLOYEE", "Employee"),
                    ("SUPERVISOR", "Supervisor"),
                    ("ME_OFFICER", "M&E Officer"),
                    ("PROGRAM_DIRECTOR", "Program Director"),
                    ("FINANCE", "Finance"),
                    ("INTERNAL_AUDIT", "Internal Audit"),
                    ("ADMIN", "Admin"),
                ],
                default="EMPLOYEE",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="requisition",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("PENDING_SUPERVISOR", "Pending Supervisor"),
                    ("PENDING_ME", "Pending M&E"),
                    ("PENDING_DIRECTOR", "Pending Director"),
                    ("PENDING_FINANCE", "Pending Finance"),
                    ("DISBURSED", "Disbursed"),
                    ("AWAITING_ACCOUNTABILITY", "Awaiting Internal Audit"),
                    ("COMPLETED", "Completed"),
                    ("REJECTED", "Rejected"),
                    ("REVISION_REQUESTED", "Revision Requested"),
                ],
                db_index=True,
                default="DRAFT",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="requisitionlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("SUBMIT", "Submit"),
                    ("APPROVE", "Approve"),
                    ("REQUEST_REVISION", "Request Revision"),
                    ("REJECT", "Reject"),
                    ("RESUBMIT", "Resubmit"),
                    ("SUBMIT_ACCOUNTABILITY", "Submit Accountability"),
                    ("FINALIZE_ACCOUNTABILITY", "Validate and Close"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="requisitionlog",
            name="new_status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("PENDING_SUPERVISOR", "Pending Supervisor"),
                    ("PENDING_ME", "Pending M&E"),
                    ("PENDING_DIRECTOR", "Pending Director"),
                    ("PENDING_FINANCE", "Pending Finance"),
                    ("DISBURSED", "Disbursed"),
                    ("AWAITING_ACCOUNTABILITY", "Awaiting Internal Audit"),
                    ("COMPLETED", "Completed"),
                    ("REJECTED", "Rejected"),
                    ("REVISION_REQUESTED", "Revision Requested"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="requisitionlog",
            name="previous_status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("PENDING_SUPERVISOR", "Pending Supervisor"),
                    ("PENDING_ME", "Pending M&E"),
                    ("PENDING_DIRECTOR", "Pending Director"),
                    ("PENDING_FINANCE", "Pending Finance"),
                    ("DISBURSED", "Disbursed"),
                    ("AWAITING_ACCOUNTABILITY", "Awaiting Internal Audit"),
                    ("COMPLETED", "Completed"),
                    ("REJECTED", "Rejected"),
                    ("REVISION_REQUESTED", "Revision Requested"),
                ],
                max_length=32,
            ),
        ),
    ]