"""Resumable original-PDF archive, parallel across learners, serial per learner."""
import json
import math
import os
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.review_pdf_inventory import (
    TABLE, discover, seed, summary, reviews_for, match_review,
)
from aptem.services.review_pdf_transfer import (
    AptemSession, ReviewBlobStore, TransferError, download_pdf, inspect_pdf,
)
from aptem.services.review_pdf_lease import ReviewArchiveLease, LeaseLost, GUARD_SQL


class Command(BaseCommand):
    help = 'Archive original Aptem review PDFs to private Azure storage. Dry-run unless --apply.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--workers', type=int, default=5)
        parser.add_argument('--scope', choices=('all','active'), default='all')
        parser.add_argument('--learner-ids', default='', help='Aptem learner IDs, comma separated.')
        parser.add_argument('--evidence-ids', default='', help='Optional pilot PDF evidence IDs.')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--run-hours', type=float, default=8,
                            help='Maximum runtime; 0 runs until the queue is finished or explicitly stopped.')
        parser.add_argument('--report', default=str(settings.BASE_DIR.parent/'debug'/'review-pdf-sync.json'))
        parser.add_argument('--stop-file', default=str(settings.BASE_DIR.parent/'debug'/'review-pdf-sync.stop'))

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('PDF archive requires PostgreSQL.')
        if options['status']:
            self.stdout.write(json.dumps(summary(),default=str))
            return
        if (not 1 <= options['workers'] <= 5 or options['limit'] < 0
                or not math.isfinite(options['run_hours']) or options['run_hours'] < 0):
            raise CommandError('Workers must be 1..5; limit >= 0; run-hours >= 0 (0 means no deadline).')
        ids = {x.strip() for x in options['learner_ids'].split(',') if x.strip()}
        rows = discover(options['scope'],ids)
        evidence_ids = {int(x.strip()) for x in options['evidence_ids'].split(',') if x.strip()}
        if evidence_ids:
            rows = [r for r in rows if r['source_evidence_id'] in evidence_ids]
        if options['limit']:
            rows = rows[:options['limit']]
        overview = {'candidate_files':len(rows),'learners':len({r['aptem_learner_id'] for r in rows}),
            'mapping':dict(Counter(r['mapping_status'] for r in rows)),
            'document_types':dict(Counter(r['document_type'] for r in rows)),
            'scope':options['scope'],'workers':options['workers']}
        self.stdout.write(json.dumps(overview,ensure_ascii=False))
        if not options['apply'] or not rows:
            return
        self.options = options
        self.report_path = Path(options['report'])
        self.report_path.parent.mkdir(parents=True,exist_ok=True)
        self.stop_path = Path(options['stop_file'])
        if self.stop_path.exists():
            raise CommandError('Stop file exists. Remove it deliberately before starting a new run.')
        self.abort = threading.Event()
        self.mutex = threading.Lock()
        self.auth = AptemSession()
        self.cooldown_until = 0.0
        self.last_transient_error = None
        self.blocker = None
        self.active = {}
        self.deadline = (time.monotonic() + options['run_hours']*3600
                         if options['run_hours'] else math.inf)
        self.started = datetime.now(timezone.utc).isoformat()
        self.lease = ReviewArchiveLease()
        if not self.lease.acquire():
            raise CommandError('A review PDF sync is already running (active coordinator lease).')
        try:
            self.scope_ids = [r['source_evidence_id'] for r in rows]
            self.checkpoint('initializing')
            seed(rows)
            # No private PDFs leave the source until write/read/privacy checks pass.
            # A temporary network outage is a cooldown, not a terminal cloud failure.
            if not self.preflight():
                self.checkpoint('stopped')
                return
            with connection.cursor() as c:
                c.execute(f'''UPDATE {TABLE} SET status='retry',next_attempt_at=now(),
                    last_error='Previous coordinator interrupted',updated_at=now()
                    WHERE status='processing' AND source_evidence_id=ANY(%s)
                    AND {GUARD_SQL}''',[self.scope_ids,*self.lease.guard_params])
            self.checkpoint('running')
            with ThreadPoolExecutor(max_workers=options['workers']) as executor:
                while not self.stopping():
                    grouped = defaultdict(list)
                    with connection.cursor() as c:
                        c.execute(f'''SELECT id,source_evidence_id,aptem_learner_id,learner_id,
                            source_url,original_filename,attempts FROM {TABLE}
                            WHERE source_evidence_id=ANY(%s) AND status IN ('pending','retry')
                            AND (next_attempt_at IS NULL OR next_attempt_at<=now())
                            ORDER BY aptem_learner_id,source_evidence_id''',
                            [self.scope_ids])
                        keys = [x[0] for x in c.description]
                        for raw in c.fetchall():
                            item = dict(zip(keys,raw))
                            grouped[item['aptem_learner_id']].append(item)
                    futures = [executor.submit(self.learner,learner,docs) for learner,docs in grouped.items()]
                    while futures and not all(f.done() for f in futures):
                        self.checkpoint('stopping' if self.stopping() else 'running')
                        time.sleep(2)
                    for future in futures:
                        future.result()
                    if self.stopping():
                        break
                    with connection.cursor() as c:
                        c.execute(f'''SELECT count(*) FROM {TABLE} WHERE source_evidence_id=ANY(%s)
                            AND status IN ('pending','retry')''', [self.scope_ids])
                        if c.fetchone()[0] == 0:
                            break
                    self.checkpoint('waiting_to_retry')
                    self.abort.wait(10)
            with connection.cursor() as c:
                c.execute(f"SELECT count(*) FROM {TABLE} WHERE source_evidence_id=ANY(%s) AND status<>'uploaded'",[self.scope_ids])
                remaining = c.fetchone()[0]
            state = ('blocked' if self.blocker else 'stopped' if self.stopping()
                     else 'finished_with_errors' if remaining else 'finished')
            self.checkpoint(state)
            self.stdout.write(json.dumps({'run_status':state,**summary()},default=str))
        except TransferError as exc:
            self.blocker = str(exc)
            self.checkpoint('blocked',renew=False)
            raise CommandError(str(exc)) from None
        except Exception as exc:
            self.abort.set()
            self.blocker = type(exc).__name__
            self.checkpoint('blocked',renew=False)
            raise CommandError('PDF sync stopped: '+type(exc).__name__) from None
        finally:
            try:
                self.lease.release()
            except Exception as exc:
                # A failed release expires naturally; never unlock somebody else's run.
                self.stderr.write('Coordinator lease release deferred: '+type(exc).__name__)

    def stopping(self):
        return self.abort.is_set() or self.stop_path.exists() or time.monotonic() >= self.deadline

    def preflight(self):
        while not self.stopping():
            store = ReviewBlobStore()
            try:
                store.preflight()
                return True
            except TransferError as exc:
                if not exc.retryable:
                    raise
                self.pause_for_transient(exc)
                self.stdout.write('[RETRY] '+str(exc))
            finally:
                store.close()
            while time.monotonic() < self.cooldown_until and not self.stopping():
                self.checkpoint('waiting_for_storage')
                self.abort.wait(2)
        return False

    def pause_for_transient(self, exc):
        with self.mutex:
            self.last_transient_error = str(exc)
            self.cooldown_until = max(self.cooldown_until,time.monotonic()+300)

    def failure_policy(self, exc, attempts):
        """Keep known temporary faults retryable; never spin on invalid files/access."""
        if isinstance(exc,LeaseLost):
            self.blocker = str(exc)
            self.abort.set()
            return False
        if isinstance(exc,TransferError):
            if exc.retryable and exc.global_block:
                self.pause_for_transient(exc)
                return True
            if exc.global_block:
                self.blocker = str(exc)
                self.abort.set()
            # With an explicit no-deadline run, retry known network interruptions too.
            if exc.retryable and math.isinf(self.deadline):
                return True
            return exc.retryable and attempts < settings.REVIEW_PDF_MAX_ATTEMPTS
        return attempts < settings.REVIEW_PDF_MAX_ATTEMPTS

    def checkpoint(self, state, *, renew=True):
        if renew:
            try:
                self.lease.renew()
            except Exception:
                # Fence this process immediately, before joining any in-flight workers.
                self.abort.set()
                raise
        with self.mutex:
            active = dict(self.active)
        data = {'run_status':state,'started_at':self.started,
            'updated_at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),
            'workers':self.options['workers'],'scope':self.options['scope'],
            'active_learners':active,'blocker':self.blocker,
            'run_hours':self.options['run_hours'],
            'last_transient_error':self.last_transient_error,
            'cooldown_seconds':max(0,round(self.cooldown_until-time.monotonic()))}
        try:
            data['summary'] = summary()
            if hasattr(self,'scope_ids'):
                with connection.cursor() as c:
                    c.execute(f'''SELECT status,count(*) FROM {TABLE}
                        WHERE source_evidence_id=ANY(%s) GROUP BY status''',[self.scope_ids])
                    data['scope_counts'] = dict(c.fetchall())
        except Exception as exc:
            data['summary_error'] = type(exc).__name__
        temporary = self.report_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data,default=str,indent=2),encoding='utf-8')
        temporary.replace(self.report_path)

    def learner(self, learner, documents):
        connection.close()
        store = None
        try:
            store = ReviewBlobStore()
            with requests.Session() as session:
                review_cache = {}
                for document in documents:
                    while time.monotonic() < self.cooldown_until and not self.stopping():
                        self.abort.wait(1)
                    if self.stopping():
                        return
                    with connection.cursor() as c:
                        c.execute(f'''UPDATE {TABLE} SET status='processing',attempts=attempts+1,
                            started_at=now(),updated_at=now() WHERE id=%s AND status IN ('pending','retry')
                            AND {GUARD_SQL}
                            RETURNING attempts''',[document['id'],*self.lease.guard_params])
                        claimed = c.fetchone()
                    if not claimed:
                        continue
                    attempts = claimed[0]
                    with self.mutex:
                        self.active[str(learner)] = document['source_evidence_id']
                    stream = None
                    try:
                        stream,size,digest = download_pdf(document,self.auth,session)
                        metadata = inspect_pdf(stream)
                        saved = store.store(document,stream,size,digest)
                        lid = document['learner_id']
                        if lid not in review_cache:
                            review_cache[lid] = reviews_for(lid)
                        review_id,match_status = match_review(document,metadata,review_cache[lid])
                        with connection.cursor() as c:
                            c.execute(f'''UPDATE {TABLE} SET status='uploaded',review_id=%s,
                                review_match_status=%s,azure_account_url=%s,azure_container=%s,
                                azure_blob_name=%s,azure_etag=%s,sha256=%s,file_size=%s,page_count=%s,
                                pdf_metadata=%s::jsonb,last_error=NULL,next_attempt_at=NULL,
                                uploaded_at=now(),verified_at=now(),updated_at=now() WHERE id=%s
                                AND {GUARD_SQL}''',
                                [review_id,match_status,*[saved[k] for k in ('azure_account_url',
                                    'azure_container','azure_blob_name','azure_etag','sha256','file_size')],
                                 metadata['page_count'],json.dumps(metadata),document['id'],*self.lease.guard_params])
                            if c.rowcount != 1:
                                raise LeaseLost('PDF archive coordinator lease was lost')
                        self.stdout.write(f'[UPLOADED] learner={learner} evidence={document["source_evidence_id"]} bytes={size} link={match_status}')
                    except Exception as exc:
                        # Database/network exception messages can contain credentials. Log class only.
                        safe = str(exc) if isinstance(exc,TransferError) else type(exc).__name__
                        retry = self.failure_policy(exc,attempts)
                        with connection.cursor() as c:
                            c.execute(f'''UPDATE {TABLE} SET status=%s,last_error=%s,
                                next_attempt_at=now()+(%s * interval '1 second'),updated_at=now()
                                WHERE id=%s AND {GUARD_SQL}''',
                                ['retry' if retry else 'failed',safe,min(60*2**min(attempts,4),600),
                                 document['id'],*self.lease.guard_params])
                        self.stdout.write(f'[ERROR] learner={learner} evidence={document["source_evidence_id"]} {safe}')
                    finally:
                        if stream is not None:
                            stream.close()
                    self.abort.wait(max(0.2,settings.APTEM_REQUEST_DELAY_SECONDS))
        finally:
            if store:
                store.close()
            with self.mutex:
                self.active.pop(str(learner),None)
            connection.close()
