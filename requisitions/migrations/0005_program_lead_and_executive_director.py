from django.db import migrations, models


def forwards_update_roles_and_statuses(apps, schema_editor):
    User = apps.get_model("requisitions", "User")
    Requisition = apps.get_model("requisitions", "Requisition")
    RequisitionLog = apps.get_model("requisitions", "RequisitionLog")

    User.objects.filter(role="PROGRAM_DIRECTOR").update(role="PROGRAM_LEAD")
    Requisition.objects.filter(status="PENDING_DIRECTOR").update(
        status="PENDING_PROGRAM_LEAD"
    )
    RequisitionLog.objects.filter(previous_status="PENDING_DIRECTOR").update(
        previous_status="PENDING_PROGRAM_LEAD"
    )
    RequisitionLog.objects.filter(new_status="PENDING_DIRECTOR").update(
        new_status="PENDING_PROGRAM_LEAD"
    )


def backwards_update_roles_and_statuses(apps, schema_editor):
    User = apps.get_model("requisitions", "User")
    Requisition = apps.get_model("requisitions", "Requisition")
    RequisitionLog = apps.get_model("requisitions", "RequisitionLog")

    User.objects.filter(role="PROGRAM_LEAD").update(role="PROGRAM_DIRECTOR")
    Requisition.objects.filter(status="PENDING_PROGRAM_LEAD").update(
        status="PENDING_DIRECTOR"
    )
    RequisitionLog.objects.filter(previous_status="PENDING_PROGRAM_LEAD").update(
        previous_status="PENDING_DIRECTOR"
    )
    RequisitionLog.objects.filter(new_status="PENDING_PROGRAM_LEAD").update(
        new_status="PENDING_DIRECTOR"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0004_remove_requisition_budget_line_remove_requisition_project"),
    ]

    operations = [
        migrations.RunPython(
            forwards_update_roles_and_statuses,
            backwards_update_roles_and_statuses,
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("EMPLOYEE", "Employee"),
                    ("SUPERVISOR", "Supervisor"),
                    ("ME_OFFICER", "M&E Officer"),
                    ("PROGRAM_LEAD", "Program Lead"),
                    ("EXECUTIVE_DIRECTOR", "Executive Director"),
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
            name="activity_type",
            field=models.CharField(
                choices=[
                    ("NON_ADMINISTRATIVE", "Programs"),
                    ("ADMINISTRATIVE", "Administrative Activity"),
                ],
                default="NON_ADMINISTRATIVE",
                max_length=32,
                verbose_name="Activity",
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
                    ("PENDING_PROGRAM_LEAD", "Pending Program Lead"),
                    ("PENDING_EXECUTIVE_DIRECTOR", "Pending Executive Director"),
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
            name="new_status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("PENDING_SUPERVISOR", "Pending Supervisor"),
                    ("PENDING_ME", "Pending M&E"),
                    ("PENDING_PROGRAM_LEAD", "Pending Program Lead"),
                    ("PENDING_EXECUTIVE_DIRECTOR", "Pending Executive Director"),
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
                    ("PENDING_PROGRAM_LEAD", "Pending Program Lead"),
                    ("PENDING_EXECUTIVE_DIRECTOR", "Pending Executive Director"),
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
