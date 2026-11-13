from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0008_production_growth"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="entra_object_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Immutable Microsoft Entra ID object identifier (oid claim).",
                max_length=64,
                null=True,
                unique=True,
                verbose_name="Entra Object ID",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="entra_tenant_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Microsoft Entra ID directory tenant identifier (tid claim).",
                max_length=64,
                verbose_name="Entra Tenant ID",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="entra_last_sync_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Last Entra SSO Sync",
            ),
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(
                fields=["entra_tenant_id", "entra_object_id"],
                name="user_entra_tid_oid_idx",
            ),
        ),
    ]
