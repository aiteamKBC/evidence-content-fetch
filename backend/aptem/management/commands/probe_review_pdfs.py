"""Download a small explicitly selected original-PDF pilot, without cloud/DB writes."""
import hashlib
import json
import shutil
import time
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from aptem.services.review_pdf_inventory import discover, match_review, reviews_for
from aptem.services.review_pdf_transfer import AptemSession, download_pdf, inspect_pdf, TransferError


class Command(BaseCommand):
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--evidence-ids', required=True)

    def handle(self, *args, **options):
        ids = {int(x.strip()) for x in options['evidence_ids'].split(',') if x.strip()}
        if not 1 <= len(ids) <= 5:
            raise CommandError('Pilot requires one to five evidence IDs.')
        rows = [r for r in discover() if r['source_evidence_id'] in ids]
        if len(rows) != len(ids):
            raise CommandError('Some pilot IDs are not generated review PDFs in the source inventory.')
        directory = settings.BASE_DIR.parent/'debug'/'review-pdf-pilot'
        directory.mkdir(parents=True,exist_ok=True)
        auth = AptemSession()
        results = []
        try:
            with requests.Session() as session:
                for row in rows:
                    start = time.monotonic()
                    stream,size,digest = download_pdf(row,auth,session)
                    with stream:
                        metadata = inspect_pdf(stream)
                        review_id,method = match_review(row,metadata,reviews_for(row['learner_id']))
                        target = directory/f'{row["source_evidence_id"]}-{digest}.pdf'
                        if not target.exists():
                            with target.open('xb') as f:
                                shutil.copyfileobj(stream,f)
                        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                            raise CommandError('Existing pilot checksum mismatch.')
                    result = {'evidence_id':row['source_evidence_id'],'aptem_learner_id':row['aptem_learner_id'],
                        'learner_id':row['learner_id'],'review_id':review_id,'review_match_status':method,
                        'bytes':size,'sha256':digest,'metadata':metadata,'seconds':round(time.monotonic()-start,2)}
                    results.append(result)
                    self.stdout.write(json.dumps(result))
        except TransferError as exc:
            raise CommandError(str(exc)) from None
        (directory/'report.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
