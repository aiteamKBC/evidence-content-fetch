"""Repair and reconcile live review inventories with resumable disjoint workers."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from aptem.management.commands.audit_active_learner_reviews import persist_review
from aptem.management.commands.import_active_learner_reviews import planned_for
from aptem.services.review_audit import active_snapshot, shard_records, checkpoint, now, access_failure
from aptem.services.review_capture import capture_review
from aptem.services.review_probe import aptem_page, check_session
from aptem.services.review_repair import finished_inventory, read_finished_identity, database_inventory, compare_inventory


class Command(BaseCommand):
    help = 'Repair reviews against a reconciled live Active learner manifest.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--worker-count', type=int, default=1)
        parser.add_argument('--worker-index', type=int, default=0)
        parser.add_argument('--learner-id')
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--manifest', default=str(settings.BASE_DIR.parent/'debug'/'live-learner-inventory.json'))

    def handle(self, *args, **options):
        records = active_snapshot()
        live = json.loads(Path(options['manifest']).read_text(encoding='utf-8'))
        live_ids = [str(r['Id']) for r in live['records'] if r['UserProgram_Status'] == 'Active']
        if len(live['records']) != live['total'] or len(live_ids) != len(set(live_ids)) or set(live_ids) != set(records):
            raise CommandError('Live Active inventory no longer matches the database; reconcile before repairing.')
        if options['learner_id']:
            records = {key: value for key,value in records.items() if key == options['learner_id']}
            if not records:
                raise CommandError('Learner is not in the verified Active inventory')
        try:
            records = shard_records(records, options['worker_count'], options['worker_index'])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        suffix = f'learner-{options["learner_id"]}' if options['learner_id'] else f'worker-{options["worker_index"]}-of-{options["worker_count"]}'
        path = settings.BASE_DIR.parent/'debug'/f'review-repair-{suffix}.json'
        if path.exists() and not options['resume']:
            raise CommandError('Repair report exists; use --resume.')
        lock_path = path.with_suffix('.json.lock')
        try:
            lock = lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CommandError('Repair worker is already running or its lock requires inspection.') from None
        try:
            previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            for key, record in records.items():
                old = previous.get('learners', {}).get(key)
                if old and old['status'] in ('verified', 'no_reviews') and old['learner_id'] == record['learner_id']:
                    records[key] = old
                else:
                    record.update(expected_reviews={}, saved_review_ids=[], errors=[], comparison=None)
            report = {'started_at': previous.get('started_at', now()), 'capture_version': 3,
                      'live_active_count': len(live_ids), 'worker_index': options['worker_index'],
                      'run_status': 'running', 'learners': records}
            checkpoint(path, report)
            for key, record in records.items():
                if record['status'] in ('verified', 'no_reviews'):
                    continue
                if record['mapping_status'] != 'matched':
                    record['status'] = 'mapping_issue'
                    checkpoint(path, report)
                    continue
                record['status'] = 'processing'
                record['started_at'] = now()
                checkpoint(path, report)

                def failure(stage, exc, review_id=None):
                    if access_failure(exc):
                        raise exc
                    record['errors'].append({'stage': stage, 'review_id': review_id,
                        'type': type(exc).__name__, 'message': re.sub(r'https?://\S+', '[URL]', str(exc).split('Call log:')[0])[:600]})
                    checkpoint(path, report)

                def save(payload):
                    persist_review(payload)
                    record['saved_review_ids'].append(str(payload['aptem_review_id']))
                    checkpoint(path, report)
                    self.stdout.write(f'[SAVED] learner={key} review={payload["aptem_review_id"]} sections={len(payload.get("sections", []))}')

                try:
                    with aptem_page() as page:
                        page.goto(f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/{key}/dashboard', wait_until='domcontentloaded')
                        check_session(page)
                        page.get_by_text('Active', exact=True).wait_for()
                        record['live_verified_at'] = now()
                        planned = []
                        try:
                            planned = planned_for(page, key)
                            record['planned_inventory_complete'] = True
                            for payload in planned:
                                rid = str(payload['aptem_review_id'])
                                record['expected_reviews'][rid] = payload['status']
                                if payload['status'] == 'In Progress':
                                    continue
                                try:
                                    save(payload)
                                except Exception as exc:
                                    failure('save_planned', exc, rid)
                        except Exception as exc:
                            failure('planned_inventory', exc)
                        for payload in planned:
                            if payload['status'] != 'In Progress':
                                continue
                            rid = str(payload['aptem_review_id'])
                            try:
                                page.goto(f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/{key}/reviews/review-content/{rid}', wait_until='domcontentloaded')
                                # Only read existing form values; never click Save, Finish, or Continue.
                                save(capture_review(page, key, rid, payload['source_metadata']))
                            except Exception as exc:
                                failure('in_progress_content', exc, rid)
                        inventory = []
                        try:
                            inventory = finished_inventory(page, key)
                            record['expected_finished_count'] = len(inventory)
                        except Exception as exc:
                            failure('finished_inventory', exc)
                        seen_finished = set()
                        for item in inventory:
                            rid = None
                            try:
                                rid = read_finished_identity(page, key, item)
                                if rid in record['expected_reviews']:
                                    raise ValueError('Duplicate review ID across live inventories')
                                seen_finished.add(rid)
                                record['expected_reviews'][rid] = 'Completed'
                                save(capture_review(page, key, rid, item['metadata']))
                            except Exception as exc:
                                failure('finished_content', exc, rid)
                        record['finished_inventory_complete'] = ('expected_finished_count' in record and len(seen_finished) == record['expected_finished_count'])
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        stored = executor.submit(database_inventory, record['learner_id']).result()
                    record['comparison'] = compare_inventory(record['expected_reviews'], stored)
                    ok = (not record['errors'] and record['planned_inventory_complete'] and record['finished_inventory_complete']
                          and not any(record['comparison'].values()))
                    record['status'] = ('verified' if record['expected_reviews'] else 'no_reviews') if ok else 'partial'
                except Exception as exc:
                    if access_failure(exc):
                        record['status'] = 'access_blocked'
                        report['run_status'] = 'blocked'
                        report['blocker'] = str(exc)
                        checkpoint(path, report)
                        raise CommandError('Aptem access blocked; saved repairs preserved.') from None
                    failure('learner', exc)
                    record['status'] = 'partial' if record['saved_review_ids'] else 'failed'
                record['checked_at'] = now()
                checkpoint(path, report)
                self.stdout.write(f'[{record["status"]}] learner={key} errors={len(record["errors"])}')
            report['run_status'] = 'finished'
            report['finished_at'] = now()
            checkpoint(path, report)
            self.stdout.write(json.dumps(report['summary']))
        finally:
            lock.close()
            lock_path.unlink()
