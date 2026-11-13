"""Management command to build a deployable Microsoft Teams app .zip package."""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from requisitions.teams import package_teams_app_zip


class Command(BaseCommand):
    help = "Generate a validated Microsoft Teams app package (.zip) containing manifest.json, color.png, and outline.png."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default=str(Path(settings.BASE_DIR) / "dist" / "financedesk-teams-app.zip"),
            help="Destination path for the generated .zip package (default: dist/financedesk-teams-app.zip).",
        )
        parser.add_argument(
            "--app-id",
            default=None,
            help="Override MICROSOFT_TEAMS_APP_ID UUID.",
        )
        parser.add_argument(
            "--client-id",
            default=None,
            help="Override MICROSOFT_CLIENT_ID UUID.",
        )
        parser.add_argument(
            "--site-url",
            default=None,
            help="Override SITE_URL (must be https:// unless --allow-http is passed).",
        )
        parser.add_argument(
            "--allow-http",
            action="store_true",
            help="Allow http:// URLs for local testing manifests.",
        )

    def handle(self, *args, **options):
        try:
            output_path = package_teams_app_zip(
                options["output"],
                app_id=options["app_id"],
                client_id=options["client_id"],
                site_url=options["site_url"],
                require_https=not options["allow_http"],
            )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        self.stdout.write(
            self.style.SUCCESS(f"Microsoft Teams app package created: {output_path}")
        )
