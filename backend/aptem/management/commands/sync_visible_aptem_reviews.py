"""Sync only review IDs proved visible in each learner's Aptem review grids.

The browser inventories both tabs and every page before any database cleanup.
Each learner is independent: a failed/incomplete inventory leaves their rows alone.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.management.commands.import_active_learner_reviews import (
    metadata, planned_for, wait_review_list, advance_review_page,
)
from aptem.services.review_api_sync import (
    ReviewSyncError, api_get, canonical_mapping, capture_is_verified,
    fetch_review_content, live_inventory, login_session, needs_content,
    save_live_review,
)
from aptem.services.review_probe import aptem_page, check_session, ProbeError
from aptem.services.classic_review_inventory import classic_visible_ids, LiveWithdrawn


def now():
    return datetime.now(timezone.utc).isoformat()


def checkpoint(path, report):
    report['updated_at'] = now()
    report['failed_learner_ids'] = [learner_id for learner_id, record in
                                    report['learners'].items()
                                    if record.get('status') == 'failed']
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def finished_ids(page, learner_id):
    """Resolve the ID behind every visible Finished View action, page by page."""
    from playwright.sync_api import expect

    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
    check_session(page)
    page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
    page.get_by_role('button', name='Finished', exact=True).click()
    page.wait_for_url(f'**/learners/{learner_id}/reviews/finished')
    if wait_review_list(page):
        return []

    inventory = []
    page_number = 1
    while True:
        rows = page.get_by_role('row').filter(
            has=page.get_by_role('button', name='View', exact=True))
        for row in rows.all():
            inventory.append((page_number, metadata(page, row)))
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            match = re.search(r'of\s+(\d+)\s+items', page.locator('kendo-pager').inner_text())
            if not match or len(inventory) != int(match[1]):
                raise ProbeError('Finished grid contains unrecognized rows')
            break
        page_number += 1
        advance_review_page(page, page_number)

    ids = []
    for index, (target_page, expected_metadata) in enumerate(inventory):
        page.goto(f'{base}/pwa/learners/{learner_id}/reviews/finished',
                  wait_until='domcontentloaded')
        check_session(page)
        if wait_review_list(page):
            raise ProbeError('Finished list became empty during ID resolution')
        for number in range(2, target_page + 1):
            advance_review_page(page, number)
        row_index = index - sum(1 for p, _ in inventory[:index] if p < target_page)
        row = page.get_by_role('row').filter(
            has=page.get_by_role('button', name='View', exact=True)).nth(row_index)
        expect(row).to_be_visible()
        if metadata(page, row) != expected_metadata:
            raise ProbeError('Finished grid changed during ID resolution')
        row.get_by_role('button', name='View', exact=True).click()
        page.wait_for_url(re.compile(
            rf'/learners/{learner_id}/reviews/review-content/\d+$'))
        ids.append(page.url.rsplit('/', 1)[1])
    return ids


def visible_ids(learner_id):
    for attempt in range(3):
        try:
            with aptem_page() as page:
                page.set_default_timeout(45000)
                page.set_default_navigation_timeout(90000)
                planned = planned_for(page, learner_id, verify_active=False)
                finished = finished_ids(page, learner_id)
            break
        except Exception as exc:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    ids = [str(row['aptem_review_id']) for row in planned] + finished
    if len(ids) != len(set(ids)) or any(not value.isdecimal() for value in ids):
        raise ProbeError('Visible review IDs are missing or duplicated')
    return ids, len(planned), len(finished)


def sync_selected(learner_id, canonical_id, ids, *, classic_only=False, refresh_visible_panels=False):
    if not ids:
        return {'saved_metadata': 0, 'saved_content': 0, 'saved_classic': 0}
    counts = {'saved_metadata': 0, 'saved_content': 0, 'saved_classic': 0}
    connection.close()
    with connection.cursor() as cursor:
        cursor.execute('''SELECT r.id,r.aptem_review_id,r.learner_id,r.status,
            r.extraction_status,coalesce(s.section_count,0),
            r.review_data->>'capture_version',r.review_data->>'capture_verified',
            r.review_data->>'content_source_updated_at'
            FROM "Learner".reviews r
            LEFT JOIN (SELECT review_id,count(*) section_count
                       FROM "Learner".review_sections GROUP BY review_id) s
              ON s.review_id=r.id
            WHERE r.aptem_review_id=ANY(%s)''', [ids])
        rows = cursor.fetchall()
    stored = {str(row[1]): {'id': row[0], 'learner_id': row[2],
              'status': row[3], 'extraction_status': row[4],
              'sections': row[5],
              'capture_version': int(row[6]) if str(row[6] or '').isdigit() else 0,
              'capture_verified': row[7] == 'true',
              'content_source_updated_at': row[8]} for row in rows}
    session = login_session()
    try:
        def resolve_batch(batch):
            try:
                return live_inventory(session, review_ids=batch), []
            except ReviewSyncError as exc:
                if (not classic_only or str(exc) not in (
                        'Aptem selected inventory does not match the requested review IDs',
                        'Aptem review inventory is empty or invalid')):
                    raise
                if len(batch) == 1:
                    return [], batch
                middle = len(batch) // 2
                left, missing_left = resolve_batch(batch[:middle])
                right, missing_right = resolve_batch(batch[middle:])
                return left + right, missing_left + missing_right

        for start in range(0, len(ids), 30):
            batch = ids[start:start + 30]
            live, classic_missing = resolve_batch(batch)
            for review in live:
                if str(review['LearnerId']) != str(learner_id):
                    raise CommandError('Visible review returned a different learner identity')
                review_id = str(review['Id'])
                previous = stored.get(review_id)
                if previous and previous['learner_id'] != canonical_id:
                    raise CommandError('Visible review belongs to another canonical learner')
                if needs_content(review, previous):
                    meta, sections = fetch_review_content(session, review_id)
                    if refresh_visible_panels:
                        from aptem.services.review_ui_supplements import enrich_visible_panels
                        meta, sections = enrich_visible_panels(learner_id, review_id, meta, sections)
                    save_live_review(review, canonical_id, meta, sections)
                    counts['saved_content'] += 1
                else:
                    save_live_review(review, canonical_id)
                    counts['saved_metadata'] += 1
            for review_id in classic_missing:
                extended = api_get(
                    session, f'/api/MWS.Review/Review/{review_id}/Extended').json()
                if (not isinstance(extended, dict)
                        or str(extended.get('id')) != review_id
                        or str(extended.get('userId')) != str(learner_id)
                        or not (extended.get('typeName') or '').strip()):
                    raise ReviewSyncError('Classic-only review identity is not verified')
                meta, sections = fetch_review_content(session, review_id)
                if refresh_visible_panels:
                    from aptem.services.review_ui_supplements import enrich_visible_panels
                    meta, sections = enrich_visible_panels(learner_id, review_id, meta, sections)
                if not refresh_visible_panels and not capture_is_verified(meta, sections):
                    raise ReviewSyncError('Classic-only review content is not fully verified')
                review = {'Id': review_id, 'LearnerId': learner_id,
                          'Name': extended.get('name') or extended['typeName'],
                          'Type': extended['typeName'],
                          'OwnerName': meta.get('ownerName'),
                          'Status': extended.get('status'),
                          'PlannedDate': extended.get('plannedStartDateTime'),
                          'CompletedDate': extended.get('completeDateTime'),
                          'UpdatedDate': extended.get('updateDateTime'),
                          'classic_extended': extended}
                save_live_review(review, canonical_id, meta, sections,
                                 source_kind='classic')
                counts['saved_classic'] += 1
    finally:
        session.close()
    return counts


def verify_saved_visible(canonical_id, ids):
    """Do not prune when a displayed, started review lacks verified content."""
    if not ids:
        return
    with connection.cursor() as cursor:
        cursor.execute('''SELECT aptem_review_id,status,extraction_status,
            review_data->>'capture_verified' FROM "Learner".reviews
            WHERE learner_id=%s AND aptem_review_id=ANY(%s)''', [canonical_id, ids])
        rows = cursor.fetchall()
    if len(rows) != len(ids) or {str(row[0]) for row in rows} != set(ids):
        raise ReviewSyncError('Some displayed reviews were not saved before cleanup')
    started = {'Completed', 'In Progress', 'InProgress',
               'Awaiting Signature', 'AwaitingSignature', 'Archived'}
    incomplete = [str(review_id) for review_id, status, extraction, verified in rows
                  if status in started and (extraction != 'success' or verified != 'true')]
    if incomplete:
        raise ReviewSyncError('Displayed started reviews have unverified content: '
                              + ','.join(incomplete))


def prune_hidden(canonical_id, ids):
    """Delete only this learner's rows absent from both complete visible grids."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SELECT id FROM "Learner".learners WHERE id=%s FOR UPDATE',
                           [canonical_id])
            if not cursor.fetchone():
                raise CommandError('Canonical learner disappeared before cleanup')
            cursor.execute('''SELECT id FROM "Learner".reviews
                WHERE learner_id=%s AND NOT (aptem_review_id=ANY(%s))''',
                           [canonical_id, ids])
            targets = [row[0] for row in cursor.fetchall()]
            if not targets:
                return {'reviews_deleted': 0, 'sections_deleted': 0,
                        'documents_deleted': 0, 'coach_links_deleted': 0,
                        'coach_signatures_deleted': 0, 'coach_documents_deleted': 0}
            cursor.execute('''SELECT id FROM "Coach".coach_imported_review_instance
                WHERE source_review_id=ANY(%s)''', [targets])
            overlays = [row[0] for row in cursor.fetchall()]
            counts = {}
            for key, sql, params in (
                ('coach_signatures_deleted',
                 'DELETE FROM "Coach".coach_migrated_review_signature WHERE overlay_id=ANY(%s)', overlays),
                ('coach_documents_deleted',
                 'DELETE FROM "Coach".coach_migrated_review_document WHERE overlay_id=ANY(%s)', overlays),
                ('coach_links_deleted',
                 'DELETE FROM "Coach".coach_imported_review_instance WHERE id=ANY(%s)', overlays),
                ('documents_deleted',
                 'DELETE FROM "Learner".review_documents WHERE review_id=ANY(%s)', targets),
                ('sections_deleted',
                 'DELETE FROM "Learner".review_sections WHERE review_id=ANY(%s)', targets),
                ('reviews_deleted',
                 'DELETE FROM "Learner".reviews WHERE id=ANY(%s)', targets),
            ):
                cursor.execute(sql, [params])
                counts[key] = cursor.rowcount
            if counts['reviews_deleted'] != len(targets):
                raise CommandError('Cleanup deleted an unexpected number of reviews')
            return counts


class Command(BaseCommand):
    help = 'Sync and retain only reviews visible in each learner Aptem Reviews UI.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--worker-count', type=int, default=1)
        parser.add_argument('--worker-index', type=int, default=0)
        parser.add_argument('--learner-id')
        parser.add_argument('--report', default=str(settings.BASE_DIR.parent / 'debug' /
                                                      'visible-aptem-reviews.json'))
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--failed-from-report',
                            help='Run only failed learners from a completed worker report.')
        parser.add_argument('--nonwithdrawn-failed-from-reports', nargs='+',
                            help='Use failed IDs from completed reports, excluding DB Withdrawn.')
        parser.add_argument('--classic-only', action='store_true',
                            help='Verify current status and inventory every Aptem Classic Reviews page.')
        parser.add_argument('--include-withdrawn', action='store_true',
                            help='Process learners whose live Aptem status is Withdrawn.')

    def sync_learner(self, learner_id, canonical_id, report, path, apply, classic_only=False,
                     include_withdrawn=False):
        prior = report['learners'].get(learner_id, {})
        record = dict(prior)
        history = list(record.get('failure_history', []))
        if prior.get('error') and not history:
            history.append({'error': prior['error'],
                            'finished_at': prior.get('finished_at')})
        record.update(status='checking_ui', started_at=now(),
                      attempt_count=prior.get('attempt_count', 1 if prior else 0) + 1,
                      failure_history=history)
        record.pop('error', None)
        record.pop('finished_at', None)
        report['learners'][learner_id] = record
        checkpoint(path, report)
        try:
            if classic_only:
                with aptem_page() as page:
                    page.set_default_timeout(45000)
                    page.set_default_navigation_timeout(90000)
                    ids, planned, finished = classic_visible_ids(
                        page, learner_id, include_withdrawn=include_withdrawn)
            else:
                ids, planned, finished = visible_ids(learner_id)
            record.update(visible_ids=ids, visible_planned=planned,
                          visible_finished=finished, status='ui_verified',
                          inventory_source='classic' if classic_only else 'pwa')
            checkpoint(path, report)
            if apply:
                saved = sync_selected(learner_id, canonical_id, ids,
                                      classic_only=classic_only)
                previous = record.get('saved', {})
                record['saved'] = {key: previous.get(key, 0) + value
                                   for key, value in saved.items()}
                checkpoint(path, report)
                verify_saved_visible(canonical_id, ids)
                deleted = prune_hidden(canonical_id, ids)
                previous = record.get('deleted', {})
                record['deleted'] = {key: previous.get(key, 0) + value
                                     for key, value in deleted.items()}
            record.update(status='complete', finished_at=now())
            self.stdout.write(f'[OK] learner={learner_id} visible={len(ids)} '
                              f'deleted={record.get("deleted", {}).get("reviews_deleted", 0)}')
        except LiveWithdrawn as exc:
            record.update(status='skipped_withdrawn', error=str(exc), finished_at=now())
            self.stdout.write(f'[SKIP] learner={learner_id} live_status=Withdrawn')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            finished_at = now()
            record.update(status='failed', error=error, finished_at=finished_at)
            record['failure_history'].append({'error': error,
                                              'finished_at': finished_at})
            self.stdout.write(f'[FAILED] learner={learner_id} {error}')
        checkpoint(path, report)

    def handle(self, *args, **options):
        count, index = options['worker_count'], options['worker_index']
        if not 1 <= count <= 5 or not 0 <= index < count:
            raise CommandError('worker-count must be 1..5 and worker-index valid')
        mapping, fallback = canonical_mapping()
        if fallback:
            # Unique email mapping is valid, but cleanup needs the persisted Aptem ID.
            mapping = {key: value for key, value in mapping.items() if key not in fallback}
        if sum(bool(options[key]) for key in ('learner_id', 'failed_from_report',
                                             'nonwithdrawn_failed_from_reports')) > 1:
            raise CommandError('Choose only one learner selection option')
        if options['learner_id']:
            learner_id = str(options['learner_id'])
            if learner_id not in mapping:
                raise CommandError('Learner is not canonically mapped by Aptem ID')
            learners = [learner_id]
        elif options['failed_from_report']:
            source_path = Path(options['failed_from_report'])
            if not source_path.is_file():
                raise CommandError(f'Failed source report does not exist: {source_path}')
            source = json.loads(source_path.read_text(encoding='utf-8'))
            if (source.get('worker_count') != count or source.get('worker_index') != index
                    or not source.get('apply') or source.get('status') not in
                    ('finished', 'finished_with_failures')):
                raise CommandError('Failed source report is not a completed matching worker run')
            failed = [str(learner_id) for learner_id in source.get('failed_learner_ids', [])]
            actual = [learner_id for learner_id, record in source.get('learners', {}).items()
                      if record.get('status') == 'failed']
            if len(failed) != len(set(failed)) or set(failed) != set(actual):
                raise CommandError('Failed source report IDs do not match failed records')
            if any(learner_id not in mapping or not learner_id.isdecimal()
                   or int(learner_id) % count != index for learner_id in failed):
                raise CommandError('Some failed learners lack a matching canonical mapping')
            learners = sorted(failed, key=int)
        elif options['nonwithdrawn_failed_from_reports']:
            if count != 1 or index != 0 or not options['classic_only']:
                raise CommandError('Non-Withdrawn failed selection requires one Classic worker')
            failed = []
            for name in options['nonwithdrawn_failed_from_reports']:
                source_path = Path(name)
                if not source_path.is_file():
                    raise CommandError(f'Failed source report does not exist: {source_path}')
                source = json.loads(source_path.read_text(encoding='utf-8'))
                actual = [key for key, value in source.get('learners', {}).items()
                          if value.get('status') == 'failed']
                selected = [str(value) for value in source.get('failed_learner_ids', [])]
                if (not source.get('apply') or source.get('status') != 'finished_with_failures'
                        or set(actual) != set(selected) or len(selected) != len(set(selected))):
                    raise CommandError('Failed source report is incomplete or inconsistent')
                failed.extend(selected)
            if len(failed) != len(set(failed)) or any(value not in mapping for value in failed):
                raise CommandError('Failed learner IDs are duplicate or lack canonical mapping')
            with connection.cursor() as cursor:
                cursor.execute('''SELECT aptem_id, programme_status FROM "Learner".learners
                    WHERE aptem_id=ANY(%s)''', [failed])
                statuses = {str(key): status for key, status in cursor.fetchall()}
            if len(statuses) != len(failed):
                raise CommandError('Some failed learners have no database status')
            learners = sorted((value for value in failed
                               if (statuses[value] or '').strip().casefold() != 'withdrawn'), key=int)
        else:
            learners = sorted((key for key in mapping if int(key) % count == index), key=int)
        path = Path(options['report'])
        if count > 1:
            path = path.with_name(f'{path.stem}-worker-{index}-of-{count}{path.suffix}')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not options['resume']:
            raise CommandError('Report exists; use --resume or choose a new report')
        report = (json.loads(path.read_text(encoding='utf-8'))
                  if options['resume'] and path.exists() else
                  {'started_at': now(), 'apply': options['apply'],
                   'worker_count': count, 'worker_index': index,
                   'target_learners': len(learners), 'learners': {}})
        if report['apply'] != options['apply'] or report['worker_count'] != count or report['worker_index'] != index:
            raise CommandError('Resume report options do not match')
        if options['failed_from_report'] or options['nonwithdrawn_failed_from_reports']:
            if report.get('source_failed_ids', learners) != learners:
                raise CommandError('Resume report failed-learner selection has changed')
            report['source_failed_ids'] = learners
            report['source_reports'] = ([str(source_path)] if options['failed_from_report'] else
                [str(Path(name)) for name in options['nonwithdrawn_failed_from_reports']])
        if report.get('classic_only', options['classic_only']) != options['classic_only']:
            raise CommandError('Resume report inventory mode has changed')
        report['classic_only'] = options['classic_only']
        if report.get('include_withdrawn', options['include_withdrawn']) != options['include_withdrawn']:
            raise CommandError('Resume report Withdrawn mode has changed')
        report['include_withdrawn'] = options['include_withdrawn']
        lock_path = path.with_suffix(path.suffix + '.lock')
        try:
            lock = lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CommandError(f'Worker already locked: {lock_path}') from None
        try:
            report['status'] = 'running'
            report['pid'] = os.getpid()
            checkpoint(path, report)
            if not report.get('main_pass_finished_at'):
                for learner_id in learners:
                    prior = report['learners'].get(learner_id, {})
                    if prior.get('status') in ('complete', 'failed'):
                        continue
                    self.sync_learner(learner_id, mapping[learner_id], report,
                                      path, options['apply'], options['classic_only'],
                                      options['include_withdrawn'])
                report['main_pass_finished_at'] = now()
                checkpoint(path, report)

            if 'retry_queue' not in report:
                report['retry_queue'] = [learner_id for learner_id in learners
                                         if report['learners'].get(learner_id, {}).get('status') == 'failed']
                report['status'] = 'retrying_failures' if report['retry_queue'] else 'running'
                checkpoint(path, report)
            for learner_id in report['retry_queue']:
                prior = report['learners'].get(learner_id, {})
                if prior.get('status') == 'complete' or prior.get('post_run_retry_finished_at'):
                    continue
                self.sync_learner(learner_id, mapping[learner_id], report,
                                  path, options['apply'], options['classic_only'],
                                  options['include_withdrawn'])
                report['learners'][learner_id]['post_run_retry_finished_at'] = now()
                checkpoint(path, report)
            report['status'] = ('finished_with_failures' if report['failed_learner_ids']
                                else 'finished')
            report['finished_at'] = now()
            checkpoint(path, report)
        finally:
            lock.close()
            lock_path.unlink(missing_ok=True)
