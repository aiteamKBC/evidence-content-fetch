"""Re-fetch source answers, preserve old records, and verify database readback."""
import gzip
import hashlib
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.services.review_api_sync import (
    CAPTURE_VERSION, STARTED, ReviewSyncError, _merge_preserved_sections, capture_is_verified, display_status, fetch_review_content,
    fetch_review_content_from_template, live_inventory, login_session,
    save_live_review,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode('utf-8')).hexdigest()


def write_backup(path, snapshot):
    """Publish only a completely written backup; interrupted writes stay temporary."""
    temporary = path.with_name(path.name + '.tmp')
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, default=str)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def comparable_sections(sections):
    return [{key: section.get(key, [] if key in ('fields', 'tables') else '')
             for key in ('section_name', 'fields', 'tables', 'raw_text')}
            for section in sections]


def read_stored(review_id, *, include_physical=False):
    physical = ''' ,coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.section_order,s.id)
        FROM "Learner".review_sections s WHERE s.review_id=r.id),'[]'::jsonb)::text''' if include_physical else ''
    with connection.cursor() as cursor:
        cursor.execute('''SELECT to_jsonb(r)::text,coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'section_name',s.section_name,'fields',s.fields_json,
                'tables',s.tables_json,'raw_text',coalesce(s.raw_text,''))
                ORDER BY s.section_order,s.id)
            FROM "Learner".review_sections s WHERE s.review_id=r.id
            ),'[]'::jsonb)::text''' + physical + ''' FROM "Learner".reviews r
            WHERE aptem_review_id=%s''', [str(review_id)])
        row = cursor.fetchone()
        if not row:
            return None
    stored = {'review': json.loads(row[0]), 'sections': json.loads(row[1])}
    if include_physical:
        stored['physical_sections'] = json.loads(row[2])
    return stored


def record_unverified(review_id, reason, *, source_failure=None):
    """Keep prior content but make an unsuccessful fresh audit explicit."""
    audit = {'verified': False, 'checked_at': now(), 'reason': reason}
    if source_failure:
        audit['source_failure'] = source_failure
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('''UPDATE "Learner".reviews SET extraction_status='partial',
            last_error=%s,review_data=coalesce(review_data,'{}'::jsonb) ||
                jsonb_build_object('content_audit',%s::jsonb),updated_at=now()
            WHERE aptem_review_id=%s''',
            [reason, json.dumps(audit), str(review_id)])


def expected_partial_sections(sections, before):
    """Reconstruct the preservation contract from the pre-write snapshot."""
    if not before:
        return sections
    old_data = decoded(before['review']['review_data']) or {}
    remaining_metadata = list(old_data.get('sections') or [])
    previous = []
    for row in before['sections']:
        index = next((i for i, old in enumerate(remaining_metadata)
                      if old.get('section_name') == row['section_name']), None)
        metadata = remaining_metadata.pop(index) if index is not None else {}
        previous.append({**metadata, **row})
    return _merge_preserved_sections(sections, previous)


def verify_readback(stored, review, learner_id, manifest, sections, before=None):
    if not stored:
        raise ReviewSyncError('Readback failed: saved review missing')
    parent = stored['review']
    if (str(parent['aptem_review_id']) != str(review['Id'])
            or parent['learner_id'] != learner_id):
        raise ReviewSyncError('Readback failed: saved review identity differs')
    data = decoded(parent['review_data'])
    if data.get('api_meta') != manifest:
        raise ReviewSyncError('Readback failed: source manifest differs')
    expected = expected_partial_sections(sections, before)
    verified = capture_is_verified(manifest, expected)
    if not verified and parent['extraction_status'] == 'success':
        raise ReviewSyncError('Readback failed: unverified capture marked successful')
    if verified and parent['extraction_status'] != 'success':
        raise ReviewSyncError('Readback failed: verified capture not marked successful')
    if comparable_sections(expected) != stored['sections']:
        detail = 'section fields/tables/text differ' if verified else 'prior content lost during partial capture'
        raise ReviewSyncError('Readback failed: '+detail)
    if data.get('sections') != expected:
        detail = 'original source section payload differs' if verified else 'prior content lost during partial capture'
        raise ReviewSyncError('Readback failed: '+detail)
    if data.get('capture_verified') is not verified:
        raise ReviewSyncError('Readback failed: capture verification flag differs')
    return signature({'manifest': manifest, 'sections': expected})


def verify_metadata_readback(stored, review, learner_id, before=None):
    """Verify inventory updates without making a new content verification claim."""
    if not stored:
        raise ReviewSyncError('Metadata readback failed: saved review missing')
    parent = stored['review']
    if (str(parent.get('aptem_review_id')) != str(review['Id'])
            or parent.get('learner_id') != learner_id):
        raise ReviewSyncError('Metadata readback failed: saved review identity differs')
    expected_data = dict(decoded(before['review'].get('review_data')) or {}) if before else {}
    expected_data.update(aptem_learner_id=str(review['LearnerId']),
                         aptem_review_id=str(review['Id']), live_odata=review)
    if decoded(parent.get('review_data')) != expected_data:
        raise ReviewSyncError('Metadata readback failed: source metadata or prior content differs')
    expected_header = {
        'review_name': (review.get('Name') or '').strip(),
        'review_type': (review.get('Type') or '').strip(),
        'reviewer_name': review.get('OwnerName'), 'status': display_status(review.get('Status')),
        'source_url': (f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/'
                       f'{review["LearnerId"]}/reviews/review-content/{review["Id"]}'),
    }
    if any(parent.get(key) != value for key, value in expected_header.items()):
        raise ReviewSyncError('Metadata readback failed: saved inventory header differs')
    for column, source_key in (('planned_scheduled_date', 'PlannedDate'),
                               ('completed_date', 'CompletedDate')):
        saved, source = parent.get(column), review.get(source_key)
        if saved != source:
            try:
                equal = (isinstance(saved, str) and isinstance(source, str)
                         and datetime.fromisoformat(saved.replace('Z', '+00:00'))
                         == datetime.fromisoformat(source.replace('Z', '+00:00')))
            except ValueError:
                equal = False
            if not equal:
                raise ReviewSyncError('Metadata readback failed: saved inventory date differs')
    previous_sections = before['sections'] if before else []
    previous_physical = before.get('physical_sections') if before else []
    if (stored['sections'] != previous_sections or previous_physical is None
            or stored.get('physical_sections') != previous_physical):
        raise ReviewSyncError('Metadata readback failed: prior physical sections changed')
    if before:
        metadata_columns = {
            'review_name', 'review_type', 'reviewer_name', 'planned_scheduled_date',
            'completed_date', 'status', 'source_url', 'review_data', 'content_hash',
            'last_seen_at', 'updated_at', 'learner_name', 'learner_email',
        }
        for key, value in before['review'].items():
            if key not in metadata_columns and parent.get(key) != value:
                raise ReviewSyncError('Metadata readback failed: prior parent content changed')
    elif (parent.get('raw_text') is not None or parent.get('last_extracted_at') is not None
          or parent.get('extraction_status') != 'partial'):
        raise ReviewSyncError('Metadata readback failed: new metadata row claims captured content')
    return signature({'review_data': expected_data, 'sections': previous_physical})


class Command(BaseCommand):
    help = 'Repair review content with source payload retention and exact readback checks.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--workers', type=int, default=5)
        parser.add_argument('--scope', choices=('archived', 'content', 'all', 'sync'), default='content')
        parser.add_argument('--review-ids', default='')
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--report', default=str(
            settings.BASE_DIR.parent/'debug'/'review-content-repair.json'))

    def handle(self, *args, **options):
        if not 1 <= options['workers'] <= 5:
            raise CommandError('workers must be 1..5')
        self.path = Path(options['report']).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = settings.BASE_DIR.parent/'debug'/'review-content-repair.lock'
        try:
            lock = self.lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CommandError('A review content repair is already locked; inspect its process.') from None
        lock.write(str(os.getpid()))
        lock.flush()
        self.mutex = threading.RLock()
        try:
            self.run(options)
        finally:
            connection.close()
            lock.close()
            self.lock_path.unlink(missing_ok=True)

    def run(self, options):
        previous = {}
        if options['resume'] and self.path.exists():
            previous = json.loads(self.path.read_text(encoding='utf-8')).get('results', {})
        self.report = {'run_status': 'inventory', 'pid': os.getpid(), 'started_at': now(),
                       'workers': options['workers'], 'scope': options['scope'],
                       'apply': options['apply'], 'counts': {}, 'active': {}, 'results': {}}
        # Do not destroy a resumable checkpoint if authentication/inventory fails.
        # Replace it only after the new inventory has been planned successfully.
        if not self.path.exists():
            self.checkpoint()
        selected = {x.strip() for x in options.get('review_ids', '').split(',') if x.strip()}
        session = login_session()
        try:
            live = (live_inventory(session, review_ids=selected)
                    if options['scope'] == 'sync' and selected else live_inventory(session))
        finally:
            session.close()
        connection.close()
        live_ids = [str(row.get('Id')) for row in live]
        if (any(rid in ('', 'None') for rid in live_ids)
                or len(live_ids) != len(set(live_ids))):
            raise CommandError('Live inventory has missing or duplicate review IDs')
        missing = selected - set(live_ids)
        if missing:
            raise CommandError('Requested reviews are missing from live inventory: '+', '.join(sorted(missing)))
        with connection.cursor() as cursor:
            cursor.execute('SELECT id,aptem_id FROM "Learner".learners WHERE aptem_id IS NOT NULL')
            learner_rows = cursor.fetchall()
            mapping = {str(aptem_id): learner_id for learner_id, aptem_id in learner_rows}
            if len(mapping) != len(learner_rows):
                raise CommandError('Multiple canonical learners share an Aptem ID')
            cursor.execute('''SELECT aptem_review_id,review_data->>'capture_version',
                review_data->'api_meta'->>'capture_verified',
                review_data->>'content_source_updated_at',extraction_status,
                review_data->>'capture_verified',review_data->'content_audit'->>'verified'
                FROM "Learner".reviews WHERE aptem_review_id IS NOT NULL''')
            existing_rows = cursor.fetchall()
            existing = {str(row[0]): row[1:] for row in existing_rows}
            if len(existing) != len(existing_rows):
                raise CommandError('Multiple stored reviews share an Aptem review ID')
        statuses = STARTED
        tasks = []
        resumed = 0
        for review in live:
            rid, aid = str(review['Id']), str(review['LearnerId'])
            if selected and rid not in selected:
                continue
            if options['scope'] == 'archived' and review.get('Status') != 'Archived':
                continue
            if options['scope'] == 'content' and review.get('Status') not in statuses:
                continue
            metadata_only = options['scope'] == 'sync' and review.get('Status') not in statuses
            if metadata_only and review.get('Status') not in {'Scheduled', 'NotScheduled'}:
                raise CommandError(f'Review {rid} has an unsupported sync status')
            if aid not in mapping:
                raise CommandError(f'Review {rid} has no exact canonical learner identity')
            old = existing.get(rid)
            if (not metadata_only and options['resume'] and previous.get(rid, {}).get('outcome') == 'verified'
                    and old and old[0] == str(CAPTURE_VERSION) and old[1] == 'true'
                    and old[2] == review.get('UpdatedDate') and old[3] == 'success'
                    and old[4] == 'true' and old[5] != 'false'):
                self.report['results'][rid] = previous[rid]
                resumed += 1
                continue
            tasks.append((review, mapping[aid]))
        self.report.update(live_total=len(live), selected_total=len(tasks)+resumed,
                           resumed_verified=resumed, pending=len(tasks))
        self.backup_dir = self.path.parent/'review-content-before'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        self.report['backup_directory'] = str(self.backup_dir)
        if options['apply']:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.stdout.write(json.dumps({k: self.report[k] for k in (
            'live_total', 'selected_total', 'resumed_verified', 'pending')}))
        if not options['apply']:
            self.report['run_status'] = 'planned'
            self.checkpoint()
            return
        self.report['run_status'] = 'running'
        self.checkpoint()
        shards = [tasks[i::options['workers']] for i in range(options['workers'])]
        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            futures = [pool.submit(self.worker, i, shard) for i, shard in enumerate(shards)]
            for future in as_completed(futures):
                future.result()
        outcomes = Counter(row['outcome'] for row in self.report['results'].values())
        self.report['run_status'] = ('finished' if set(outcomes) <= {'verified', 'metadata_verified'}
                                     else 'finished_with_gaps')
        self.report['finished_at'] = now()
        self.checkpoint()
        self.stdout.write(json.dumps({'run_status': self.report['run_status'],
                                      'counts': self.report['counts']}))

    def worker(self, index, tasks):
        connection.close()
        session = None
        try:
            for review, learner_id in tasks:
                rid = str(review['Id'])
                metadata_only = (self.report.get('scope') == 'sync'
                                 and review.get('Status') not in STARTED)
                with self.mutex:
                    self.report['active'][str(index)] = rid
                try:
                    manifest, sections = None, None
                    if not metadata_only:
                        if session is None:
                            session = login_session()
                        try:
                            manifest, sections = fetch_review_content(session, rid)
                        except ReviewSyncError:
                            if rid != '14980':
                                raise
                            manifest, sections = fetch_review_content_from_template(session, rid, '14979')
                        if (str(manifest.get('id')) != rid
                                or str(manifest.get('userId')) != str(review['LearnerId'])):
                            raise ReviewSyncError('Source manifest review/learner identity does not match inventory')
                    result = self.save_and_verify(review, learner_id, manifest, sections,
                                                  metadata_only=metadata_only)
                except Exception as exc:
                    result = {'outcome': 'error', 'source_status': review.get('Status'),
                              'type': type(exc).__name__,
                              'reason': str(exc)[:350] if isinstance(exc, ReviewSyncError) else type(exc).__name__}
                    if isinstance(exc, ReviewSyncError) and getattr(exc, 'diagnostics', None):
                        result['source_failure'] = exc.diagnostics
                    connection.close()
                    try:
                        backup = self.backup_dir/f'{rid}.json.gz'
                        if not backup.exists():
                            before = read_stored(rid)
                            write_backup(backup, before)
                        if not metadata_only:
                            record_unverified(rid, 'Content verification failed: '+result['reason'],
                                              source_failure=result.get('source_failure'))
                    except Exception as audit_exc:
                        result['audit_record_error'] = type(audit_exc).__name__
                with self.mutex:
                    self.report['results'][rid] = result
                    self.report['active'].pop(str(index), None)
                self.checkpoint()
        finally:
            if session is not None:
                session.close()
            connection.close()

    def save_and_verify(self, review, learner_id, manifest, sections, *, metadata_only=False):
        rid = str(review['Id'])
        sync = self.report.get('scope') == 'sync'
        connection.close_if_unusable_or_obsolete()
        # In sync mode, a failed readback rolls back the entire record. Existing
        # scopes keep their original transaction/audit behavior.
        with transaction.atomic() if sync else nullcontext():
            if sync:
                with connection.cursor() as cursor:
                    cursor.execute('''SELECT id FROM "Learner".reviews
                        WHERE aptem_review_id=%s FOR UPDATE''', [rid])
            before = read_stored(rid, include_physical=True) if metadata_only else read_stored(rid)
            write_backup(self.backup_dir/f'{rid}.json.gz', before)
            save_live_review(review, learner_id, manifest, sections)
            after = read_stored(rid, include_physical=True) if metadata_only else read_stored(rid)
            if metadata_only:
                digest = verify_metadata_readback(after, review, learner_id, before)
                return {
                    'outcome': 'metadata_verified', 'content_verified': False,
                    'source_status': review.get('Status'), 'source_updated': review.get('UpdatedDate'),
                    'stored_section_count': len(after['sections']),
                    'previous_sections': len(before['sections']) if before else 0,
                    'rendered_content_changed': False, 'source_metadata_sha256': digest,
                    'verified_at': now(),
                }
            digest = verify_readback(after, review, learner_id, manifest, sections, before)
            return {
                'outcome': 'verified' if after['review']['review_data'].get('capture_verified') else 'unverified',
                'source_status': review.get('Status'), 'source_updated': review.get('UpdatedDate'),
                'section_count': len(sections), 'stored_section_count': len(after['sections']),
                'nonempty_sections': sum(bool(s.get('fields') or s.get('tables')) for s in sections),
                'previous_sections': len(before['sections']) if before else 0,
                'rendered_content_changed': (not before or before['sections'] != comparable_sections(sections)),
                'source_capture_sha256': digest,
                'section_states': dict(Counter(s.get('capture_state', 'unknown') for s in sections)),
                'issues': manifest.get('capture_errors', []),
                'preserved_section_issues': [s.get('capture_error') for s in
                    after['review']['review_data'].get('sections', [])
                    if s.get('capture_state') == 'preserved_unverified'],
                'verified_at': now(),
            }

    def checkpoint(self):
        with self.mutex:
            self.report['updated_at'] = now()
            self.report['counts'] = dict(Counter(
                r['outcome'] for r in self.report['results'].values()))
            temp = self.path.with_suffix(f'.tmp.{os.getpid()}')
            try:
                temp.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')
                for attempt in range(10):
                    try:
                        temp.replace(self.path)
                        return
                    except PermissionError:
                        time.sleep(0.05 * (attempt+1))
            except OSError:
                pass
            self.stderr.write('Checkpoint write deferred; database capture continues.')
