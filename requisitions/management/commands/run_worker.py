"""Management command to run the durable background task worker."""

import signal
import time

from django.core.management.base import BaseCommand

from requisitions.tasks import process_pending_tasks


class Command(BaseCommand):
    help = "Process queued background tasks (emails, notifications, PDF cache warming)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process currently due tasks once and exit.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Maximum number of tasks to process per polling cycle (default: 50).",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=2.0,
            help="Seconds to wait between queue polls when idle (default: 2.0).",
        )

    def handle(self, *args, **options):
        run_once = options["once"]
        batch_size = max(1, options["batch_size"])
        poll_interval = max(0.1, options["poll_interval"])
        stop_requested = False

        def _handle_stop(signum, frame):
            nonlocal stop_requested
            stop_requested = True
            self.stdout.write(self.style.WARNING("Shutdown signal received; stopping worker..."))

        signal.signal(signal.SIGINT, _handle_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_stop)

        if run_once:
            completed, failed = process_pending_tasks(limit=batch_size)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Worker cycle finished: {completed} completed, {failed} failed/retried."
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("Starting FinanceDesk background worker..."))
        while not stop_requested:
            completed, failed = process_pending_tasks(limit=batch_size)
            if completed or failed:
                self.stdout.write(
                    f"Processed batch: {completed} completed, {failed} failed/retried."
                )
            else:
                time.sleep(poll_interval)
