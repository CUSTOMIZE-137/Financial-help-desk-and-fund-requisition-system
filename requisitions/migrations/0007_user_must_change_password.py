from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0006_user_signature_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="must_change_password",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, the user must set a new password upon signing in before accessing the workspace.",
                verbose_name="Require password change on next sign-in",
            ),
        ),
    ]
