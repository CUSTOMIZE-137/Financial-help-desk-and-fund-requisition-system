import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("requisitions", "0005_program_lead_and_executive_director"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="signature_image",
            field=models.FileField(
                blank=True,
                help_text="Upload a PNG or JPEG image of your signature to be stamped on approved requisitions.",
                upload_to="signatures/%Y/%m/%d/",
                validators=[
                    django.core.validators.FileExtensionValidator(["png", "jpg", "jpeg"])
                ],
                verbose_name="Signature Image",
            ),
        ),
    ]
