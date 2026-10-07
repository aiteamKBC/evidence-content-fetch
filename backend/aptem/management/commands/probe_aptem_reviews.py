from django.core.management.base import BaseCommand, CommandError

from aptem.services.review_probe import run_probe


class Command(BaseCommand):
    help = "Read-only headed probe: Betty Heaton -> Reviews -> Finished -> first View."
    requires_system_checks = []

    def handle(self, *args, **options):
        try:
            run_probe(self.stdout.write)
        except Exception as exc:
            raise CommandError(f"Review probe failed ({type(exc).__name__}): {exc}") from exc
