"""Resolve every remaining unknown signature snapshot without importing content."""
import copy
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.review_api_sync import api_get
from aptem.services.review_signature_completion import CompletionClient
from aptem.services.review_signatures import (
    SignatureError, apply_rows, export_followups, fingerprint, inventory,
    load_verified, write_verified,
)


class Command(BaseCommand):
    help = 'Read direct document inventories and existing PDFs for unresolved PR/MCM signatures'

    def add_arguments(self, parser):
        parser.add_argument('--run-dir', required=True)
        parser.add_argument('--prior-source-dir', required=True)
        parser.add_argument('--review-id')
        parser.add_argument('--workers', type=int, default=4, choices=range(1, 5))
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        folder = Path(options['run_dir']).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        if (folder/'recovery.json').exists():
            raise CommandError('Use a new run directory; existing recovery files must be retained')
        rows = (load_verified(folder/'inventory.json') if options['resume'] and (folder/'inventory.json').exists()
                else [r for r in inventory() if r['before'] and r['before']['state']=='unknown'
                    and (not options['review_id'] or r['aptem_review_id']==options['review_id'])])
        if not rows:
            self.stdout.write('No unresolved reviews in scope')
            return
        with connection.cursor() as cursor:
            cursor.execute('SELECT id,status FROM "Learner".reviews WHERE id=ANY(%s)',[[r['id'] for r in rows]])
            statuses = dict(cursor.fetchall())
        rows.sort(key=lambda r: (statuses[r['id']] in ('Scheduled','Not Scheduled'), r['id']))
        write_verified(folder/'inventory.json', rows)
        self.stdout.write(f'Scope: {len(rows)} unresolved reviews', ending='\n')
        self.stdout.flush()
        programs = {}
        for learner in {r['aptem_learner_id'] for r in rows}:
            path = Path(options['prior_source_dir'])/(learner+'.json')
            if path.exists():
                prior = load_verified(path)
                for group in prior.get('groups', []):
                    programs.setdefault(str(group.get('ItemId')), set()).add(group['ProgramId'])
        metadata = {}
        client = CompletionClient()
        try:
            # Aptem enforces a 300-node OData filter limit (100 IDs exceed it).
            for start in range(0, len(rows), 50):
                batch = rows[start:start+50]
                requested = {r['aptem_review_id'] for r in batch}
                data = api_get(client.session, '/odata/1.0/Reviews', params={
                    '$filter':' or '.join('Id eq '+r['aptem_review_id'] for r in batch),
                    '$select':'Id,LearnerId,Status,OwnerId,OwnerName,Type',
                    '$count':'true', '$top':100, '$orderby':'Id'}, timeout=60).json()
                values = data.get('value')
                count = data.get('@odata.count',data.get('odata.count'))
                if (not isinstance(values,list) or count is None or len(values)!=int(count)
                        or any(str(v['Id']) not in requested for v in values)
                        or len({v['Id'] for v in values})!=len(values)):
                    raise CommandError('Selected source review metadata inventory is incomplete')
                metadata.update({str(v['Id']):v for v in values})
                if start % 1000 == 0:
                    self.stdout.write(f'Identity metadata: {min(start+50,len(rows))}/{len(rows)}')
                    self.stdout.flush()
        finally:
            client.close()
        write_verified(folder/'source-metadata.json', metadata)
        local = threading.local()
        clients = []

        def collect(row):
            path = folder/'source'/(row['aptem_review_id']+'.json')
            if options['resume'] and path.exists():
                cached = load_verified(path)
                age = (datetime.now(timezone.utc)-datetime.fromisoformat(cached['after']['checked_at'])).total_seconds()
                if 0 <= age <= 3600 and not cached.get('error'):
                    return row, cached
            try:
                if not hasattr(local, 'client'):
                    local.client = CompletionClient()
                    clients.append(local.client)
                value = local.client.resolve(row,metadata.get(row['aptem_review_id']),
                    sorted(programs.get(row['aptem_review_id'],[])),folder)
            except Exception as exc:
                after = copy.deepcopy(row['before'])
                after.update(checked_at=datetime.now(timezone.utc).isoformat(),
                             reason='direct_verification_failed',verified=False)
                value = {'after':after,'error':str(exc) if isinstance(exc,SignatureError) else type(exc).__name__}
            write_verified(path,value)
            return row,value

        errors = {}
        states = Counter()
        try:
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                futures = [pool.submit(collect,row) for row in rows]
                for done,future in enumerate(as_completed(futures),1):
                    row,value = future.result()
                    row['after'] = value['after']
                    states[row['after']['state']]+=1
                    if value.get('error'):
                        errors[row['aptem_review_id']]=value['error']
                    if done%100==0 or done==len(rows) or value.get('error'):
                        self.stdout.write(f'Checked {done}/{len(rows)}: {dict(states)}; errors={len(errors)}')
                        self.stdout.flush()
        finally:
            for client in clients:
                client.close()
        before = fingerprint()
        write_verified(folder/'recovery.json',{'version':1,'key':'signature_status',
            'created_at':datetime.now(timezone.utc).isoformat(),'fingerprint_before':before,'rows':rows})
        saved = load_verified(folder/'recovery.json')
        report = {'reviews':len(rows),'states':dict(states),'errors':errors,
            'unknown_reasons':dict(Counter(r['after'].get('reason') for r in rows if r['after']['state']=='unknown')),
            'applied':False,'updated':0}
        write_verified(folder/'report.json',report)
        export_followups(folder,rows)
        if options['apply']:
            for start in range(0,len(rows),100):
                report['updated']+=apply_rows(saved['rows'][start:start+100])
                if start%1000==0:
                    self.stdout.write(f'Applied/read back {min(start+100,len(rows))}/{len(rows)}')
                    self.stdout.flush()
            report.update(applied=True,fingerprint_after=fingerprint())
            report['existing_content_unchanged']=report['fingerprint_after']==before
            write_verified(folder/'report.json',report)
        self.stdout.write(json.dumps(report,indent=2))
