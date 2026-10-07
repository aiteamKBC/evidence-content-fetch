from django.core.management.base import BaseCommand
from django.db import transaction

from aptem.models import AptemEvidence
from aptem.services.evidence_downloader import AptemEvidenceDownloader, wait_between_evidence_files
from aptem.services.evidence_source import fetch_source_evidence_items
from aptem.services.processor import process_evidence_payload


class Command(BaseCommand):
    help = "Process PDF file evidence records from fetching_evidence.evidence_items sequentially; notes are excluded."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--all", action="store_true", help="Keep processing until no unprocessed source PDFs remain.")
        parser.add_argument("--batch-size", type=int, default=50)
        parser.add_argument("--include-processed", action="store_true")

    def handle(self, *args, **options):
        total_target = None if options["all"] else max(int(options["limit"]), 1)
        batch_size = max(int(options["batch_size"]), 1)
        processed_count = 0
        successful = 0
        failed = 0
        downloader = AptemEvidenceDownloader()

        while total_target is None or processed_count < total_target:
            remaining = batch_size if total_target is None else min(batch_size, total_target - processed_count)
            items = fetch_source_evidence_items(
                remaining,
                include_processed=options["include_processed"],
            )
            if not items:
                self.stdout.write("No unprocessed PDF source evidence remains.")
                break

            self.stdout.write(f"Selected batch: {len(items)}")
            for index, payload in enumerate(items):
                with transaction.atomic():
                    record = process_evidence_payload(payload, downloader=downloader)

                processed_count += 1
                if record.status == AptemEvidence.STATUS_ANALYSED:
                    successful += 1
                else:
                    failed += 1

                self.stdout.write(
                    (
                        f"{processed_count}: evidence_id={record.evidence_id}, "
                        f"status={record.status}, words={record.word_count}, "
                        f"reflection={record.has_reflection}, ksbs={record.has_ksbs}, "
                        f"quality={record.quality_check_status}"
                    )
                )
                self.stdout.flush()

                if index < len(items) - 1:
                    wait_between_evidence_files()

        self.stdout.write("Done.")
        self.stdout.write(f"Processed: {processed_count}")
        self.stdout.write(f"Successful: {successful}")
        self.stdout.write(f"Failed: {failed}")
        for record in AptemEvidence.objects.order_by("-updated_at")[: min(processed_count, 10)]:
            self.stdout.write(
                (
                    f"{record.evidence_id}: {record.status}, words={record.word_count}, "
                    f"reflection={record.has_reflection}, ksbs={record.has_ksbs}, "
                    f"quality={record.quality_check_status}"
                )
            )
