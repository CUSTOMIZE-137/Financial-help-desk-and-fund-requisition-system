from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0007_user_must_change_password"),
    ]

    operations = [
        migrations.CreateModel(
            name="BackgroundTask",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("task_name", models.CharField(db_index=True, max_length=120)),
                ("payload", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=24,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3)),
                ("last_error", models.TextField(blank=True)),
                (
                    "scheduled_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["scheduled_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["status", "scheduled_at"],
                        name="bgtask_status_sched_idx",
                    ),
                ],
            },
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(
                fields=["role", "department"], name="user_role_dept_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(
                fields=[
                    "delegate",
                    "is_active",
                    "delegate_start_date",
                    "delegate_end_date",
                ],
                name="user_active_delegate_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requisition",
            index=models.Index(
                fields=["status", "department", "activity_type"],
                name="req_status_dept_act_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requisition",
            index=models.Index(
                fields=["requestor", "-requested_at"],
                name="req_requestor_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requisition",
            index=models.Index(
                fields=["requested_at", "status"],
                name="req_reqdate_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requisitionlog",
            index=models.Index(
                fields=["requisition", "-timestamp"],
                name="reqlog_req_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requisitionlog",
            index=models.Index(
                fields=["actor", "requisition"],
                name="reqlog_actor_req_idx",
            ),
        ),
    ]
