"""Reclassify proven retained aliases without fetching or changing source answers."""
import copy
import gzip
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.management.commands.repair_review_content import (
    comparable_sections, decoded, expected_partial_sections, now, read_stored, verify_readback,
)
from aptem.services.review_api_sync import (
    CAPTURE_VERSION, VERIFIED_SECTION_STATES, capture_is_verified,
    live_inventory, login_session, save_live_review,
)


def reconciliation_plan(before, source, canonical_aptem_id):
    """Return fresh, unchanged captures only when every retained row is proved redundant."""
    if not before:
        return None, 'stored_review_missing'
    if not source:
        return None, 'source_review_missing'
    parent = before['review']
    data = decoded(parent.get('review_data')) or {}
    manifest = data.get('api_meta') or {}
    rid, learner = str(source.get('Id')), str(source.get('LearnerId'))
    if (str(parent.get('aptem_review_id')) != rid
            or str(canonical_aptem_id) != learner
            or str(data.get('aptem_review_id')) != rid
            or str(data.get('aptem_learner_id')) != learner
            or str(manifest.get('id')) != rid or str(manifest.get('userId')) != learner):
        return None, 'identity_mismatch'
    stored_source = data.get('live_odata') or {}
    if (any(stored_source.get(key) != source.get(key)
            for key in ('Id', 'LearnerId', 'Status', 'UpdatedDate'))
            or not source.get('UpdatedDate')
            or data.get('content_source_updated_at') != source.get('UpdatedDate')):
        return None, 'source_changed_or_freshness_unproven'
    expected_status = {'InProgress': 'In Progress', 'NotScheduled': 'Not Scheduled'}.get(
        source.get('Status'), source.get('Status'))
    if parent.get('status') != expected_status:
        return None, 'stored_status_differs'
    if (data.get('capture_version') != 6 or data.get('content_capture_version') != 6
            or data.get('capture_verified') is not False
            or parent.get('extraction_status') != 'partial'
            or manifest.get('capture_verified') is not True):
        return None, 'not_eligible_v6_partial_capture'
    if manifest.get('manifest_reconstructed') or manifest.get('capture_errors'):
        return None, 'source_manifest_unverified'
    audit = data.get('content_audit') or {}
    if (audit.get('verified') is False and audit.get('reason') !=
            'Prior sections restored from backup; auxiliary source verification pending'):
        return None, 'non_preservation_audit_failure'
    base, supplemental, captured = (manifest.get('sections'),
                                    manifest.get('supplemental_sections'), data.get('sections'))
    if not isinstance(base, list) or not base or not isinstance(supplemental, list) or not isinstance(captured, list):
        return None, 'source_inventory_invalid'
    count = len(base) + len(supplemental)
    if len(captured) <= count:
        return None, 'no_preserved_extra_sections'
    if comparable_sections(captured) != before['sections']:
        return None, 'stored_json_table_mismatch'
    fresh, retained = captured[:count], captured[count:]
    if (not all(section.get('capture_state') == 'preserved_unverified' for section in retained)
            or not all(section.get('capture_state') in VERIFIED_SECTION_STATES for section in fresh)):
        return None, 'partial_capture_has_other_failures'
    descriptors = [((item.get('customName') or item.get('name') or '').strip(),
                    (item.get('contentRoute') or {}).get('action'), False) for item in base]
    descriptors += [(item.get('section_name'), item.get('source_action'), True) for item in supplemental]
    for section, (label, action, is_supplemental) in zip(fresh, descriptors):
        if (section.get('section_name') != label or section.get('source_action') != action
                or 'source_payload' not in section
                or (is_supplemental and section.get('source_kind') != 'supplemental')):
            return None, 'source_section_identity_mismatch'
    if not capture_is_verified(manifest, fresh):
        return None, 'base_capture_unverified'
    fresh = copy.deepcopy(fresh)
    merged = expected_partial_sections(fresh, before)
    if not capture_is_verified(manifest, merged):
        return None, 'preserved_sections_still_unverified'
    if merged != fresh:
        return None, 'reconciliation_would_change_source_capture'
    return {'manifest': copy.deepcopy(manifest), 'sections': fresh,
            'retained_count': len(retained)}, None


def write_backup(directory, review_id, before):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f'{review_id}.json.gz'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as raw, gzip.open(raw, 'wt', encoding='utf-8') as stream:
        json.dump(before, stream, ensure_ascii=False, default=str)
    return path


def reconcile_one(review_id, source, *, apply, backup_directory):
    with transaction.atomic(), connection.cursor() as cursor:
        if not apply:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        sql = '''SELECT r.id,l.aptem_id FROM "Learner".reviews r
            LEFT JOIN "Learner".learners l ON l.id=r.learner_id
            WHERE r.aptem_review_id=%s'''
        cursor.execute(sql + (' FOR UPDATE OF r' if apply else ''), [review_id])
        identity = cursor.fetchone()
        if not identity:
            return {'outcome': 'skipped', 'reason': 'stored_review_missing'}
        before = read_stored(review_id)
        plan, reason = reconciliation_plan(before, source, identity[1])
        if plan is None:
            return {'outcome': 'skipped', 'reason': reason}
        result = {'outcome': 'eligible', 'retained_aliases': plan['retained_count'],
                  'source_section_count': len(plan['sections'])}
        if not apply:
            return result
        write_backup(backup_directory, review_id, before)
        save_live_review(source, before['review']['learner_id'], plan['manifest'], plan['sections'])
        cursor.execute('UPDATE "Learner".reviews SET last_extracted_at=%s WHERE id=%s',
                       [before['review'].get('last_extracted_at'), identity[0]])
        after = read_stored(review_id)
        digest = verify_readback(after, source, before['review']['learner_id'],
                                 plan['manifest'], plan['sections'], before)
        if (after['review'].get('last_extracted_at') != before['review'].get('last_extracted_at')
                or after['review']['review_data'].get('sections') != plan['sections']
                or after['review']['review_data'].get('capture_verified') is not True):
            raise CommandError('Reconciliation readback differed; transaction rolled back')
        result.update(outcome='reconciled', source_capture_sha256=digest,
                      capture_timestamp_preserved=True)
        return result


class Command(BaseCommand):
    help = 'Reconcile proved v6 historical aliases; no source section requests or answer changes.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--workers', type=int, default=5)
        parser.add_argument('--review-ids', default='')
        parser.add_argument('--report', default=str(
            settings.BASE_DIR.parent / 'debug' / 'review-preserved-reconciliation.json'))

    def handle(self, *args, **options):
        if CAPTURE_VERSION != 6:
            raise CommandError('This command requires the reviewed v6 capture contract')
        if not 1 <= options['workers'] <= 5:
            raise CommandError('workers must be 1..5')
        selected = {value.strip() for value in options['review_ids'].split(',') if value.strip()}
        if any(not value.isdecimal() for value in selected):
            raise CommandError('review-ids must be comma-separated numeric Aptem review IDs')
        path = Path(options['report']).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_directory = settings.BASE_DIR.parent / 'debug' / 'review-reconciliation-before' / (
            now().replace(':', '').replace('+', '_') + f'-{os.getpid()}')
        lock_path = settings.BASE_DIR.parent / 'debug' / 'review-content-repair.lock'
        lock = None
        if options['apply']:
            try:
                lock = lock_path.open('x', encoding='utf-8')
            except FileExistsError:
                raise CommandError('Review content repair is locked; reconciliation apply must wait') from None
            lock.write(str(os.getpid()))
            lock.flush()
        try:
            session = login_session()
            try:
                current = {str(row['Id']): row for row in live_inventory(session)}
            finally:
                session.close()
            connection.close()
            if not selected:
                with connection.cursor() as cursor:
                    cursor.execute('''SELECT aptem_review_id FROM "Learner".reviews
                        WHERE review_data->>'capture_version'='6'
                        AND review_data->'api_meta'->>'capture_verified'='true'
                        AND review_data->>'capture_verified'='false'
                        AND extraction_status='partial' AND aptem_review_id IS NOT NULL''')
                    selected = {str(row[0]) for row in cursor.fetchall()}
            if any(not value.isdecimal() for value in selected):
                raise CommandError('Stored Aptem review identity is not numeric; refusing backup paths')
            report = {'apply': options['apply'], 'started_at': now(), 'run_status': 'running',
                      'inventory_count': len(current), 'selected_count': len(selected),
                      'backup_directory': str(backup_directory) if options['apply'] else None,
                      'workers': options['workers'], 'results': {}, 'counts': {}}
            mutex = threading.Lock()

            def checkpoint():
                report['updated_at'] = now()
                report['counts'] = dict(Counter(row['outcome'] for row in report['results'].values()))
                temporary = path.with_suffix(f'.tmp.{os.getpid()}')
                try:
                    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
                    temporary.replace(path)
                except OSError:
                    self.stderr.write('Reconciliation report write deferred; database work continues.')

            checkpoint()

            def group(review_ids):
                connection.close()
                try:
                    for rid in review_ids:
                        try:
                            result = reconcile_one(rid, current.get(rid), apply=options['apply'],
                                                   backup_directory=backup_directory)
                        except Exception as exc:
                            result = {'outcome': 'error', 'reason': type(exc).__name__}
                            connection.close()
                        with mutex:
                            report['results'][rid] = result
                            checkpoint()
                finally:
                    connection.close()

            ordered = sorted(selected, key=int)
            shards = [ordered[i::options['workers']] for i in range(options['workers'])]
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                list(pool.map(group, shards))
            status = ('finished_with_errors' if report['counts'].get('error') else
                      'finished_with_skips' if report['counts'].get('skipped') else 'finished')
            report.update(run_status=status, finished_at=now())
            checkpoint()
            self.stdout.write(json.dumps({key: report[key] for key in
                                         ('apply', 'selected_count', 'counts', 'run_status')}))
        finally:
            connection.close()
            if lock is not None:
                lock.close()
                lock_path.unlink(missing_ok=True)
