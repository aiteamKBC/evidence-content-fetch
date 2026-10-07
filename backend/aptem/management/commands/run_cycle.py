from django.core.management.base import BaseCommand

from aptem.services.cycle import CycleService


class Command(BaseCommand):
    help = (
        "One safe reviews-sync cycle: reconcile Aptem reviews to Neon, then archive "
        "review PDFs to Azure. Guarded by a database lease; full-scope PDF pass once per day."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-apply",
            dest="apply",
            action="store_false",
            help="Dry run: pass through without --apply so nothing is written.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap items per stage (0 = no cap). Use a small value for a test cycle.",
        )
        parser.add_argument("--workers", type=int, default=5)
        parser.add_argument(
            "--scope",
            choices=("all", "active"),
            default=None,
            help="Force PDF archive scope; default follows the daily full/incremental window.",
        )
        parser.add_argument(
            "--run-hours",
            type=float,
            default=8,
            help="Max runtime for the PDF archive stage; 0 means run until the queue drains.",
        )
        parser.add_argument("--skip-reviews", action="store_true")
        parser.add_argument("--skip-pdfs", action="store_true")

    def handle(self, *args, **options):
        service = CycleService(
            options={
                "apply": options["apply"],
                "limit": options["limit"],
                "workers": options["workers"],
                "scope": options["scope"],
                "run_hours": options["run_hours"],
                "skip_reviews": options["skip_reviews"],
                "skip_pdfs": options["skip_pdfs"],
            }
        )
        result = service.run_once()
        self.stdout.write(str(result))
