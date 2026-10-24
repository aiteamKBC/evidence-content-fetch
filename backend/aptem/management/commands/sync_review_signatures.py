"""Signatures-only enrichment. Collection is read-only unless --apply is passed."""
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from aptem.services.review_signatures import (
    SignatureClient, SignatureError, apply_rows, classify, export_followups, fingerprint,
    inventory, load_verified, write_verified,
)


class Command(BaseCommand):
    help = 'Enrich existing PR/MCM metadata with per-role Aptem signatures; never imports review content.'

    def add_arguments(self, parser):
        parser.add_argument('--run-dir', required=True)
        parser.add_argument('--learner-id', help='Optional Aptem learner ID for a pilot')
        parser.add_argument('--workers', type=int, default=2, choices=range(1, 5))
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--apply-staged', action='store_true', help='Apply/resume a verified collection from the last hour')
        parser.add_argument('--restore', action='store_true', help='Restore this run only, preserving later edits')
        parser.add_argument('--resume', action='store_true', help='Reuse recent verified source captures')

    def handle(self, *args, **options):
        folder = Path(options['run_dir']).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / 'recovery.json'
        try:
            if options['apply_staged']:
                if options['restore'] or options['apply'] or options['learner_id']:
                    raise CommandError('--apply-staged is a standalone mode')
                recovery = load_verified(backup)
                for row in recovery['rows']:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['after']['checked_at'])).total_seconds()
                    if not 0 <= age <= 3600:
                        raise CommandError('Staged source evidence is stale; collect a new run')
                report = load_verified(folder / 'report.json')
                before = fingerprint()
                count = 0
                for start in range(0, len(recovery['rows']), 100):
                    count += apply_rows(recovery['rows'][start:start + 100])
                report.update(applied=True, updated=count, fingerprint_after=fingerprint())
                report['existing_content_unchanged'] = report['fingerprint_after'] == before
                write_verified(folder / 'report.json', report)
                export_followups(folder, recovery['rows'])
                self.stdout.write(json.dumps(report, indent=2))
                return
            if options['restore']:
                if options['apply'] or options['learner_id']:
                    raise CommandError('--restore cannot be combined with --apply or --learner-id')
                recovery = load_verified(backup)
                count = 0
                for start in range(0, len(recovery['rows']), 100):
                    count += apply_rows(recovery['rows'][start:start + 100], restore=True)
                write_verified(folder / 'restore-result.json', {'restored': count, 'fingerprint': fingerprint()})
                self.stdout.write(f'Restored {count} signature metadata values')
                return
            if backup.exists():
                raise CommandError('Run directory already has a recovery file; use a new directory to collect again')
            rows = inventory(options['learner_id'])
            if not rows:
                raise CommandError('No matching existing reviews')
            write_verified(folder / 'inventory.json', rows)
            learners = sorted({r['aptem_learner_id'] for r in rows}, key=int)
            self.stdout.write(f'Scope: {len(rows)} reviews / {len(learners)} learners', ending='\n')
            local = threading.local()
            clients = []

            def collect(learner):
                capture = folder / 'source' / f'{learner}.json'
                if options['resume'] and capture.exists():
                    value = load_verified(capture)
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['checked_at'])).total_seconds()
                    if 0 <= age <= 3600 and not value.get('error'):
                        return learner, value
                try:
                    if not hasattr(local, 'client'):
                        local.client = SignatureClient()
                        clients.append(local.client)
                    value = local.client.learner(learner)
                except Exception as exc:
                    # Never log source response bodies, cookies or credential-bearing URLs.
                    value = {'aptem_learner_id': learner,
                        'checked_at': datetime.now(timezone.utc).isoformat(),
                        'error': str(exc) if isinstance(exc, SignatureError) else type(exc).__name__}
                write_verified(capture, value)
                return learner, value

            evidence = {}
            try:
                with ThreadPoolExecutor(max_workers=options['workers']) as executor:
                    futures = [executor.submit(collect, learner) for learner in learners]
                    for completed, future in enumerate(as_completed(futures), 1):
                        learner, value = future.result()
                        evidence[learner] = value
                        if completed % 10 == 0 or completed == len(learners) or value.get('error'):
                            self.stdout.write(f'Collected {completed}/{len(learners)} learners; errors='
                                f'{sum(bool(v.get("error")) for v in evidence.values())}')
                            self.stdout.flush()
            finally:
                for client in clients:
                    client.close()
            for row in rows:
                row['after'] = classify(row['aptem_review_id'], row['aptem_learner_id'],
                                        evidence[row['aptem_learner_id']])
            before = fingerprint()
            recovery = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                        'key': 'signature_status', 'fingerprint_before': before, 'rows': rows}
            write_verified(backup, recovery)
            # Load the actual recovery file rather than trusting the in-memory copy.
            recovery = load_verified(backup)
            report = {'learners': len(learners), 'reviews': len(rows),
                'source_errors': {k: v['error'] for k, v in evidence.items() if v.get('error')},
                'states': dict(Counter(r['after']['state'] for r in rows)),
                'by_type': {}, 'unknown_reasons': dict(Counter(r['after'].get('reason') for r in rows if r['after']['state'] == 'unknown')),
                'missing_roles': dict(Counter(role for r in rows for role in r['after']['missing_roles'])),
                'applied': False, 'updated': 0}
            for kind in sorted({r['review_type'] for r in rows}):
                report['by_type'][kind] = dict(Counter(r['after']['state'] for r in rows if r['review_type'] == kind))
            write_verified(folder / 'report.json', report)
            export_followups(folder, rows)
            if options['apply']:
                for start in range(0, len(rows), 100):
                    report['updated'] += apply_rows(recovery['rows'][start:start + 100])
                    if start % 1000 == 0:
                        self.stdout.write(f'Applied/read back {min(start+100,len(rows))}/{len(rows)} reviews')
                        self.stdout.flush()
                report.update(applied=True, fingerprint_after=fingerprint())
                report['existing_content_unchanged'] = report['fingerprint_after'] == before
                write_verified(folder / 'report.json', report)
            self.stdout.write(json.dumps(report, indent=2))
        except SignatureError as exc:
            raise CommandError(str(exc)) from None
