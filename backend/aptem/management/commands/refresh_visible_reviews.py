"""Durable local worker coordinator; fresh visible inventory on every cycle."""
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.visible_review_cycle import (
    AlreadyRunning, local_lock, load_verified, now, run_worker, save, scope,
)


class Command(BaseCommand):
    help = 'Refresh visible reviews and signatures additively with local workers (no pruning)'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--workers', type=int, default=4, choices=range(1, 5))
        parser.add_argument('--root', default='../debug/visible-review-cycles')
        parser.add_argument('--learner-id', help='Optional single-learner pilot')
        parser.add_argument('--worker-index', type=int)
        parser.add_argument('--run-dir')
        parser.add_argument('--retry-from', help='Completed run directory whose unresolved learners need retry')

    def handle(self, *args, **options):
        root = Path(options['root']).resolve()
        root.mkdir(parents=True, exist_ok=True)
        # Same normal Edge mode as the existing local import job. Aptem's gateway
        # rejects the headless browser before the authenticated application loads.
        os.environ['APTEM_BROWSER_HEADLESS'] = 'false'
        settings.APTEM_BROWSER_HEADLESS = False
        if options['worker_index'] is not None:
            if not options['run_dir'] or not 0 <= options['worker_index'] < options['workers']:
                raise CommandError('Invalid internal worker arguments')
            if run_worker(Path(options['run_dir']), options['worker_index'], options['workers'], root/'locks'):
                raise CommandError('Some learners need retry; see worker report')
            return
        try:
            with local_lock(root/'coordinator.lock'):
                self.run_cycle(root, options)
        except AlreadyRunning:
            self.stdout.write('Another visible-review cycle is running; skipped')

    def run_cycle(self, root, options):
        folder = root/(now().replace(':', '').replace('+', '_') + f'-{os.getpid()}')
        folder.mkdir()
        mapping = scope()
        previous_complete = 0
        baseline = len(mapping)
        if options['retry_from']:
            prior = Path(options['retry_from']).resolve()
            prior_summary = load_verified(prior/'summary.json')
            if prior_summary.get('status') not in ('complete', 'needs_retry'):
                raise CommandError('Retry requires a finished source cycle')
            prior_scope = load_verified(prior/'scope.json')
            records = {}
            for path in prior.glob('worker-*.json'):
                records.update(load_verified(path)['learners'])
            targets = {key for key in prior_scope if records.get(key, {}).get('status') != 'complete'}
            if any(key not in mapping or mapping[key] != prior_scope[key] for key in targets):
                raise CommandError('Retry learner identity differs from the prior scope')
            previous_complete = prior_summary.get('previous_complete', 0) + sum(
                row.get('status') == 'complete' for row in records.values())
            baseline = prior_summary.get('baseline_learners', len(prior_scope))
            mapping = {key: value for key, value in mapping.items() if key in targets}
        if options['learner_id']:
            mapping = {key: value for key, value in mapping.items() if key == options['learner_id']}
        if not mapping:
            raise CommandError('No learners in scope')
        save(folder/'scope.json', mapping)
        connection.close()
        summary = {'started_at': now(), 'run_dir': str(folder), 'learners': len(mapping),
                   'workers': options['workers'], 'status': 'running', 'deletion_enabled': False,
                   'retry_from': options['retry_from'], 'baseline_learners': baseline,
                   'previous_complete': previous_complete}
        save(root/'latest.json', summary)
        children, logs = [], []
        try:
            for index in range(options['workers']):
                out = (folder/f'worker-{index}.log').open('ab', buffering=0)
                logs.append(out)
                command = [sys.executable, '-u', str(Path.cwd()/'manage.py'), 'refresh_visible_reviews',
                           '--root', str(root), '--workers', str(options['workers']),
                           '--worker-index', str(index), '--run-dir', str(folder)]
                child = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                children.append(child)
                time.sleep(3)
            summary['pids'] = [child.pid for child in children]
            while True:
                totals = Counter()
                visible = updated = 0
                for index in range(len(children)):
                    path = folder/f'worker-{index}.json'
                    if path.exists():
                        try:
                            report = load_verified(path)
                        except (OSError, ValueError):
                            continue  # worker may be between replacing JSON and checksum
                        for row in report['learners'].values():
                            totals[row['status']] += 1
                            visible += row.get('visible', 0)
                            updated += row.get('signatures', {}).get('updated', 0)
                codes = [child.poll() for child in children]
                summary.update(updated_at=now(), progress=dict(totals), visible_reviews=visible,
                               signatures_updated=updated, exit_codes=codes,
                               overall_complete=previous_complete+totals['complete'])
                done = all(code is not None for code in codes)
                if done:
                    summary.update(finished_at=now(), status='complete' if all(code == 0 for code in codes)
                                   and totals['complete'] == len(mapping) else 'needs_retry')
                save(folder/'summary.json', summary)
                save(root/'latest.json', summary)
                if done:
                    break
                time.sleep(20)
        finally:
            # Retain the coordinator lock until launched workers finish, even if a later spawn fails.
            for child in children:
                child.wait()
            for out in logs:
                out.close()
        self.stdout.write(json.dumps(summary, indent=2))
        if summary['status'] != 'complete':
            raise CommandError('Cycle recorded unresolved learners for the next fresh cycle')
