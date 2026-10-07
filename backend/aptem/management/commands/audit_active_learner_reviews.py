"""Audit every Active learner and persist each fetched review independently."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.management.commands.import_active_learner_reviews import planned_for, finished_for
from aptem.services.review_audit import active_snapshot, classify, checkpoint, now, access_failure, shard_records
from aptem.services.review_probe import aptem_page, check_session
from aptem.services.review_storage import save_review


def persist_review(payload):
    # Playwright owns an asyncio loop on the browser thread. Keep Django's
    # synchronous transaction on a separate thread, and propagate write failures.
    def write():
        try:
            return save_review(payload)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(write).result()


class Command(BaseCommand):
    help = 'Audit Active learner mapping and fetch reviews with resumable local checkpoints.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--fetch', action='store_true')
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--worker-count', type=int, default=1)
        parser.add_argument('--worker-index', type=int, default=0)
        parser.add_argument('--report', default=str(settings.BASE_DIR.parent / 'debug' / 'active-review-audit.json'))

    def handle(self, *args, **options):
        try:
            shard_records({}, options['worker_count'], options['worker_index'])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        path = Path(options['report'])
        if options['worker_count'] > 1:
            path = path.with_name(f'{path.stem}-worker-{options["worker_index"]}-of-{options["worker_count"]}{path.suffix}')
        if path.exists() and not options['resume']:
            raise CommandError('Report exists; use --resume or choose a new --report path.')
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + '.lock')
        try:
            lock = lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CommandError(f'Audit lock exists: {lock_path}. Check for a running worker before removing a stale lock.') from None
        try:
            self.run_audit(path, options)
        finally:
            lock.close()
            lock_path.unlink()

    def run_audit(self, path, options):
        records = active_snapshot()
        records = shard_records(records, options.get('worker_count', 1), options.get('worker_index', 0))
        previous = json.loads(path.read_text(encoding='utf-8')) if options['resume'] and path.exists() else {}
        for key, old in previous.get('learners', {}).items():
            if key not in records or old['learner_id'] != records[key]['learner_id']:
                continue
            # Resume only trusts completion proven in this audit, never historic row counts.
            if previous.get('scanner_version') == 2 and old['status'] in ('complete', 'no_reviews'):
                records[key] = old
            else:
                records[key]['previous_attempts'] = old.get('attempts', 0) + old.get('previous_attempts', 0)
        report = {'started_at': previous.get('started_at', now()), 'scanner_version': 2,
                  'scope': 'database Active learners',
                  'worker_count': options.get('worker_count', 1), 'worker_index': options.get('worker_index', 0),
                  'run_status': 'running' if options['fetch'] else 'snapshot_only',
                  'learners': records, 'blocker': None}
        checkpoint(path, report)
        if not options['fetch']:
            self.stdout.write(json.dumps(report['summary']))
            return
        for key, record in records.items():
            if record['status'] in ('complete', 'no_reviews'):
                continue
            record['attempts'] += 1
            record['status'] = 'processing'
            checkpoint(path, report)

            def save(payload):
                # Unresolved identities can be inspected but must never be written to a guessed learner.
                if record['mapping_status'] != 'matched':
                    return
                review_id = str(payload['aptem_review_id'])
                if review_id in record['saved_review_ids']:
                    raise ValueError('Duplicate review identity across live inventories')
                # save_review may deliberately refuse an individual payload (e.g. Planned
                # metadata that would clobber a stored Completed). Record the rejection
                # and continue the inventory scan; halting on one refusal skips reviews
                # further down the same list that may actually be new.
                try:
                    persist_review(payload)
                except ValueError as exc:
                    record['errors'].append({'category': 'save', 'aptem_review_id': review_id, 'reason': str(exc)})
                    checkpoint(path, report)
                    return
                record['saved_review_ids'].append(review_id)
                if payload['status'] == 'Completed':
                    record['saved_completed_ids'].append(review_id)
                if payload['status'] == 'In Progress' and not payload.get('sections'):
                    record['in_progress_metadata_only_ids'].append(review_id)
                checkpoint(path, report)

            def inventory(count):
                record['expected_finished'] = count
                checkpoint(path, report)

            try:
                with aptem_page() as page:
                    page.goto(f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/{key}/dashboard',
                              wait_until='domcontentloaded')
                    check_session(page)
                    from playwright.sync_api import expect
                    # Missing Active text is an unverified failure, not proof of inactive status.
                    expect(page.get_by_text('Active', exact=True)).to_be_visible()
                    record['live_active_verified_at'] = now()
                    for category, operation in (
                        ('planned', lambda: planned_for(page, key, on_review=save)),
                        ('finished', lambda: finished_for(page, key, on_review=save, on_inventory=inventory)),
                    ):
                        try:
                            payloads = operation()
                            record[category + '_inventory_complete'] = True
                            record[category + '_found'] = len(payloads)
                        except Exception as exc:
                            if access_failure(exc):
                                raise
                            record['errors'].append({'category': category, 'type': type(exc).__name__})
                        checkpoint(path, report)
                    record['status'] = classify(record)
            except Exception as exc:
                if access_failure(exc):
                    record['status'] = 'access_blocked'
                    report['run_status'] = 'blocked'
                    report['blocker'] = {'learner_id': key, 'reason': str(exc), 'at': now()}
                    checkpoint(path, report)
                    self.stdout.write(json.dumps(report['summary']))
                    raise CommandError(f'Aptem access blocked; remaining learners are unverified. Report: {path}') from None
                record['errors'].append({'category': 'learner', 'type': type(exc).__name__})
                record['status'] = classify(record)
            record['checked_at'] = now()
            checkpoint(path, report)
            self.stdout.write(f'[{record["status"]}] learner {key}')
        report['run_status'] = 'finished'
        report['finished_at'] = now()
        checkpoint(path, report)
        self.stdout.write(json.dumps(report['summary']))
