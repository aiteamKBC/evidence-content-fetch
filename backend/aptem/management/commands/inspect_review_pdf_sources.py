"""Read-only inventory diagnostics. Never prints credentials or signed URLs."""
import json
import os
from collections import Counter
from importlib.util import find_spec
from urllib.parse import urlsplit, parse_qs

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        out = {"azure_sdk_installed": find_spec("azure") is not None}
        with connection.cursor() as c:
            c.execute("""SELECT table_schema, table_name, column_name, data_type
                FROM information_schema.columns
                WHERE (table_schema='fetching_evidence' AND table_name='evidence_items')
                   OR (table_schema='Learner' AND table_name IN ('reviews','review_documents','learners'))
                ORDER BY table_schema,table_name,ordinal_position""")
            out['columns'] = c.fetchall()
            c.execute("""SELECT evidence_id,learner_id,full_name,evidence_name,source_file_url
                FROM fetching_evidence.evidence_items
                WHERE evidence_kind='File' AND evidence_name ILIKE 'Review - %.pdf'
                ORDER BY learner_id,evidence_id""")
            rows = c.fetchall()
            out['source_url_shapes'] = list(Counter((urlsplit(r[4] or '').hostname,
                '/'.join(urlsplit(r[4] or '').path.split('/')[:-1])) for r in rows).items())[:20]
            out['sample'] = [r[:4] for r in rows[:8]]
            c.execute('SELECT "ID","FullName","Email","Program-Status" FROM "LMS"."Aptem_users"')
            users = {str(r[0]): r for r in c.fetchall()}
            out['source_identity_checks'] = {
                'files':len(rows), 'learners':len({str(r[1]) for r in rows}),
                'aptem_id_name_matches': sum(str(r[1]) in users and (r[2] or '').strip().casefold()==(users[str(r[1])][1] or '').strip().casefold() for r in rows),
                'active_files_by_aptem_id':sum(str(r[1]) in users and users[str(r[1])][3]=='Active' for r in rows),
                'active_learners_by_aptem_id':len({str(r[1]) for r in rows if str(r[1]) in users and users[str(r[1])][3]=='Active'}),
            }
            c.execute('SELECT id,aptem_review_id,learner_id,review_name,review_type,review_data FROM "Learner".reviews ORDER BY id LIMIT 4')
            out['review_samples'] = [{'id':r[0], 'aptem_review_id':r[1], 'learner_id':r[2],
                'name':r[3], 'type':r[4], 'payload_keys':list((r[5] or {}).keys()) if isinstance(r[5],dict) else []} for r in c.fetchall()]
        sas = urlsplit(os.environ.get('AZURE_REVIEWS_CONTAINER_URL',''))
        conn = dict(part.split('=',1) for part in os.environ.get('AZURE_STORAGE_CONNECTION_STRING','').split(';') if '=' in part)
        out['azure_configuration'] = {'reviews_host':sas.hostname,'reviews_container':sas.path,
            'sas_permissions':parse_qs(sas.query).get('sp'), 'sas_expiry':parse_qs(sas.query).get('se'),
            'connection_account':conn.get('AccountName'),'connection_has_key':bool(conn.get('AccountKey')),
            'storage_container':os.environ.get('AZURE_STORAGE_CONTAINER')}
        self.stdout.write(json.dumps(out,default=str,indent=2))
