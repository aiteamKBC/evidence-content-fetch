"""Reconcile every Aptem review belonging to a canonical Neon learner."""
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.review_api_sync import (
    ReviewSyncError, _is_transient, canonical_mapping, fetch_review_content,
    live_inventory, login_session, needs_content, save_live_review, stored_inventory,
)


def now():
    return datetime.now(timezone.utc).isoformat()


class Command(BaseCommand):
    help = 'Sync all Aptem reviews to Neon with exact learner/review identities.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--workers', type=int, default=5)
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--review-ids', default='')
        parser.add_argument('--aptem-learner-ids', default='')
        parser.add_argument('--report', default=str(settings.BASE_DIR.parent/'debug'/'all-aptem-reviews-sync.json'))

    def handle(self, *args, **options):
        if not 1 <= options['workers'] <= 5 or options['limit'] < 0:
            raise CommandError('workers must be 1..5 and limit must be non-negative')
        self.path = Path(options['report'])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix+'.lock')
        try:
            lock = self.lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CommandError('Review sync lock exists; inspect the running process before retrying.') from None
        try:
            self.run(options)
        finally:
            lock.close()
            self.lock_path.unlink(missing_ok=True)

    def run(self, options):
        session = login_session()
        try:
            live = live_inventory(session)
        finally:
            session.close()
        # The OData inventory can take more than a minute; do not reuse a Neon
        # connection that sat idle throughout that network request.
        connection.close()
        mapping, fallback = canonical_mapping()
        stored = stored_inventory()
        selected_ids = {value.strip() for value in options['review_ids'].split(',') if value.strip()}
        selected_learner_ids = {
            value.strip() for value in options['aptem_learner_ids'].split(',') if value.strip()}
        mapped, unresolved = [], Counter()
        for review in live:
            rid, aid = str(review['Id']), str(review.get('LearnerId'))
            if selected_ids and rid not in selected_ids:
                continue
            if selected_learner_ids and aid not in selected_learner_ids:
                continue
            if aid not in mapping:
                unresolved[aid] += 1
                continue
            mapped.append((review, mapping[aid]))
        if options['limit']:
            mapped = mapped[:options['limit']]
        plan = Counter()
        for review, _ in mapped:
            existing = stored.get(str(review['Id']))
            plan['content_fetch' if needs_content(review, existing) else 'metadata_only'] += 1
            plan['insert' if existing is None else 'update'] += 1
        report = {'run_status':'planned','started_at':now(),'updated_at':now(),
            'pid':os.getpid(),'workers':options['workers'],'apply':options['apply'],
            'live_total':len(live),'mapped_reviews':len(mapped),
            'mapped_learners':len({learner for _,learner in mapped}),
            'safe_email_learner_updates':len(set(fallback) & {str(r.get('LearnerId')) for r,_ in mapped}),
            'unresolved_reviews':sum(unresolved.values()),'unresolved_learners':len(unresolved),
            'plan':dict(plan),'counts':{},'active':{},'errors':[]}
        self.report = report
        self.mutex = threading.Lock()
        self.retry = []  # (review, learner_id) tuples that failed on a transient response
        self.checkpoint()
        self.stdout.write(json.dumps({key:report[key] for key in (
            'live_total','mapped_reviews','mapped_learners','unresolved_reviews','unresolved_learners','plan')}))
        if not options['apply']:
            return
        # Persist only unique-email mappings that still have no Aptem identity.
        with connection.cursor() as cursor:
            for aptem_id, learner_id in fallback.items():
                cursor.execute('''UPDATE "Learner".learners SET aptem_id=%s,updated_at=now()
                    WHERE id=%s AND aptem_id IS NULL''', [aptem_id, learner_id])
        shards = [[] for _ in range(options['workers'])]
        for review, learner_id in mapped:
            shards[int(review['Id']) % options['workers']].append((review, learner_id))
        report['run_status'] = 'running'
        self.checkpoint()
        with ThreadPoolExecutor(max_workers=options['workers']) as executor:
            futures = [executor.submit(self.worker, index, shard, stored)
                       for index, shard in enumerate(shards)]
            for future in futures:
                future.result()
        # Beat transient Aptem 5xx: re-attempt only the temporarily-failed reviews
        # serially, after a cooldown, so a struggling endpoint gets a quiet retry.
        self.retry_transient_failures(stored)
        report['run_status'] = 'finished_with_errors' if report['errors'] else 'finished'
        report['finished_at'] = now()
        self.checkpoint()
        self.stdout.write(json.dumps({'run_status':report['run_status'],
                                      'counts':report['counts'],'errors':len(report['errors'])}))

    def checkpoint(self):
        with self.mutex if hasattr(self, 'mutex') else threading.Lock():
            self.report['updated_at'] = now()
            temp = self.path.with_suffix(
                self.path.suffix+f'.tmp.{os.getpid()}.{threading.get_ident()}')
            temp.write_text(json.dumps(self.report,ensure_ascii=False,indent=2),encoding='utf-8')
            # On Windows an editor, indexer or antivirus can briefly hold the
            # destination open. A progress-file collision must never terminate
            # the database sync itself.
            for attempt in range(10):
                try:
                    temp.replace(self.path)
                    return
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            self.report['checkpoint_write_failures'] = (
                self.report.get('checkpoint_write_failures', 0) + 1)
            temp.unlink(missing_ok=True)

    def _capture_and_save(self, session, review, learner_id, stored):
        """Fetch (if needed), persist one review, and classify the stored result."""
        rid = str(review['Id'])
        api_meta = sections = None
        content_unavailable = False
        if needs_content(review, stored.get(rid)):
            try:
                api_meta, sections = fetch_review_content(session, rid)
            except ReviewSyncError as exc:
                # Aptem's ReviewContent endpoint returns 5xx for some reviews
                # (typically archived / never-completed ones) even after retries:
                # the content is simply not served. Persist the review's metadata
                # so it still exists in Neon, and flag it as upstream-unavailable
                # rather than a hard failure. It stays unverified, so the next
                # cycle retries it automatically and it heals if Aptem recovers.
                if exc.status_code in (500, 502, 503, 504):
                    content_unavailable = True
                else:
                    raise
        pk = save_live_review(review, learner_id, api_meta, sections)
        if content_unavailable:
            return 'content_unavailable_upstream'
        result = 'metadata_saved'
        if sections is not None:
            # A verified base manifest may still leave historical sections
            # unverified after the preservation merge.
            with connection.cursor() as cursor:
                cursor.execute('''SELECT extraction_status,review_data->>'capture_verified'
                    FROM "Learner".reviews WHERE id=%s''', [pk])
                saved = cursor.fetchone()
            result = 'content_saved' if saved == ('success', 'true') else 'content_partial'
        return result

    def _tally_result(self, rid, result):
        with self.mutex:
            self.report['counts'][result] = self.report['counts'].get(result, 0) + 1
            if result == 'content_partial':
                self.report['errors'].append({
                    'review_id': rid, 'type': 'UnverifiedContent',
                    'reason': 'Some sections could not be verified; prior content retained.',
                })

    def worker(self, index, tasks, stored):
        connection.close()
        session = login_session()
        try:
            for review, learner_id in tasks:
                rid = str(review['Id'])
                with self.mutex:
                    self.report['active'][str(index)] = rid
                try:
                    result = self._capture_and_save(session, review, learner_id, stored)
                    self._tally_result(rid, result)
                except Exception as exc:
                    safe = str(exc) if isinstance(exc, ReviewSyncError) else type(exc).__name__
                    with self.mutex:
                        self.report['counts']['failed'] = self.report['counts'].get('failed',0)+1
                        self.report['errors'].append({'review_id':rid,'type':type(exc).__name__,
                                                     'reason':safe[:300]})
                        # Queue only transient failures (Aptem 5xx / request drop)
                        # for the serial retry sweep; real data errors stay put.
                        if _is_transient(exc) or (isinstance(exc, ReviewSyncError)
                                                  and 'Aptem request failed' in str(exc)):
                            self.retry.append((review, learner_id))
                finally:
                    with self.mutex:
                        self.report['active'].pop(str(index),None)
                self.checkpoint()
        finally:
            session.close()
            connection.close()

    def retry_transient_failures(self, stored, *, rounds=1, cooldown=30):
        """Serially re-attempt reviews that failed on a transient Aptem response.

        Each round waits out a cooldown first (so a struggling endpoint can
        recover), then retries the queue with a single session. A review that
        succeeds is cleared from the error list and counted as recovered; one
        that keeps failing transiently is re-queued for the next round; a newly
        non-transient failure keeps its original error and is dropped from retry.
        """
        for round_no in range(rounds):
            with self.mutex:
                pending, self.retry = self.retry, []
            if not pending:
                return
            self.report.setdefault('retry_rounds', []).append(
                {'round': round_no + 1, 'pending': len(pending), 'at': now()})
            self.checkpoint()
            time.sleep(cooldown)
            connection.close()
            session = login_session()
            try:
                for review, learner_id in pending:
                    rid = str(review['Id'])
                    with self.mutex:
                        self.report['active']['retry'] = rid
                    try:
                        result = self._capture_and_save(session, review, learner_id, stored)
                        self._clear_failure(rid)
                        self._tally_result(rid, result)
                    except Exception as exc:
                        if _is_transient(exc) or (isinstance(exc, ReviewSyncError)
                                                  and 'Aptem request failed' in str(exc)):
                            with self.mutex:
                                self.retry.append((review, learner_id))
                        # else: keep the original recorded error, stop retrying it.
                    finally:
                        with self.mutex:
                            self.report['active'].pop('retry', None)
                    self.checkpoint()
            finally:
                session.close()
                connection.close()

    def _clear_failure(self, rid):
        """Drop a prior hard-failure error for rid and adjust the failed count."""
        with self.mutex:
            kept = [e for e in self.report['errors'] if e.get('review_id') != rid]
            if len(kept) != len(self.report['errors']):
                self.report['errors'] = kept
                self.report['counts']['failed'] = max(
                    0, self.report['counts'].get('failed', 0) - 1)
                self.report['counts']['retry_recovered'] = (
                    self.report['counts'].get('retry_recovered', 0) + 1)
