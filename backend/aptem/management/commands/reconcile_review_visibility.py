"""Backfill current membership from checksum-verified local browser inventories."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.review_signatures import load_verified
from aptem.services.review_visibility import reconcile_batch, timestamp
from aptem.services.visible_review_cycle import AlreadyRunning, local_lock, now, save, scope


class Command(BaseCommand):
    help = 'Mark visible/historical reviews without deleting content or changing signatures.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--root', default=str(settings.BASE_DIR.parent/'debug'/'visible-review-cycles'))
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--learner-id', action='append')
        parser.add_argument('--workers', type=int, choices=range(1, 5), default=4)

    def handle(self, *args, **options):
        root = Path(options['root']).resolve()
        mapping = scope()
        if options['learner_id']:
            mapping = {k: v for k, v in mapping.items() if k in set(options['learner_id'])}
        if not mapping:
            raise CommandError('No learners in scope')
        # Discover paths once; re-read each learner's files under its worker lock.
        candidates = {}
        for path in root.glob('*/worker-*/*/attempt-*/visible.json'):
            learner_id = path.parent.parent.name
            if learner_id in mapping:
                candidates.setdefault(learner_id, []).append(path)
        out = root/('visibility-' + now().replace(':', '').replace('+', '_'))
        out.mkdir()
        report = {'apply': options['apply'], 'started_at': now(), 'learners': {}}
        def shard(items):
            results, specs = {}, []
            try:
                with ExitStack() as locks:
                    for learner_id, canonical_id in items:
                        try:
                            locks.enter_context(local_lock(root/'locks'/f'learner-{learner_id}.lock'))
                        except AlreadyRunning:
                            results[learner_id] = {'status': 'busy'}
                            continue
                        try:
                            path = newest(learner_id, canonical_id)
                            specs.append((learner_id, canonical_id, path, out/f'{learner_id}-before.json'))
                        except Exception as exc:
                            results[learner_id] = {'status': 'failed', 'error': str(exc)[:500]}
                    try:
                        updated = reconcile_batch(specs, apply=options['apply'])
                        results.update({key: {'status': 'complete', **value} for key, value in updated.items()})
                    except Exception as exc:
                        results.update({key: {'status': 'failed', 'error': str(exc)[:500]}
                                        for key, _, _, _ in specs})
                return list(results.items())
            finally:
                connection.close()

        def newest(learner_id, canonical_id):
            eligible = []
            for path in candidates.get(learner_id, []):
                run_scope = load_verified(path.parents[3]/'scope.json')
                if run_scope.get(learner_id) != canonical_id:
                    raise ValueError('Snapshot canonical identity changed')
                snapshot = load_verified(path)
                eligible.append((timestamp(snapshot['checked_at']), path))
            if not eligible:
                raise ValueError('No verified browser inventory; leave membership unknown')
            return max(eligible, key=lambda item: item[0])[1]

        # Each connection is owned by one thread; four bounded shards avoid
        # reconnecting for every learner. Reports are written only by this thread.
        items = list(mapping.items())
        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            # Small groups make progress durable while keeping connections bounded.
            futures = [pool.submit(shard, items[i:i+10]) for i in range(0, len(items), 10)]
            for future in as_completed(futures):
                report['learners'].update(future.result())
                save(out/'report.json', report)
        report['finished_at'] = now()
        save(out/'report.json', report)
        rows = list(report['learners'].values())
        self.stdout.write(str({'report': str(out/'report.json'),
            'complete': sum(r['status'] == 'complete' for r in rows),
            'busy': sum(r['status'] == 'busy' for r in rows),
            'failed': sum(r['status'] == 'failed' for r in rows),
            'visible': sum(r.get('visible', 0) for r in rows),
            'historical': sum(r.get('historical', 0) for r in rows)}))
        if any(r['status'] != 'complete' for r in rows):
            raise CommandError('Some learners were not reconciled; see report')
