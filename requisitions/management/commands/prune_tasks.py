"""Management command to prune old completed and failed background tasks."""

from django.core.management.base import BaseCommand

from requisitions.tasks import prune_old_tasks


class Command(BaseCommand):
    help = "Delete COMPLETED and FAILED BackgroundTask rows older than the specified retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Retention period in days for completed/failed tasks (default: 30).",
        )

    def handle(self, *args, **options):
        days = max(1, options["days"])
        deleted = prune_old_tasks(days=days)
        self.stdout.write(
            self.style.SUCCESS(
                f"Pruned {deleted} completed/failed background task(s) older than {days} days."
            )
        )
