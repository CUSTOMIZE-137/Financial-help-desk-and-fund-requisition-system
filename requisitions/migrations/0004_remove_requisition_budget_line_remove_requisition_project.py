from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0003_internal_audit_review"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="requisition",
            name="budget_line",
        ),
        migrations.RemoveField(
            model_name="requisition",
            name="project",
        ),
        migrations.DeleteModel(
            name="BudgetLine",
        ),
        migrations.DeleteModel(
            name="Project",
        ),
    ]
