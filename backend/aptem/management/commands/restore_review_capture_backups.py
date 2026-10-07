"""Restore only nonempty sections omitted by a capture, without undoing new answers."""
import copy
import gzip
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.management.commands.repair_review_content import read_stored, now
from aptem.services.review_api_sync import normalized


def omitted_sections(before, current):
    remaining = list(current['sections'])
    old_metadata = list((before['review'].get('review_data') or {}).get('sections') or [])
    omitted = []
    for prior in before['sections']:
        index = next((i for i, row in enumerate(remaining)
                      if normalized(row['section_name']) == normalized(prior['section_name'])), None)
        meta_index = next((i for i, row in enumerate(old_metadata)
                           if row.get('section_name') == prior['section_name']), None)
        meta = old_metadata.pop(meta_index) if meta_index is not None else {}
        if index is not None:
            remaining.pop(index)
            continue
        if not (prior.get('fields') or prior.get('tables') or prior.get('raw_text')):
            continue
        restored = {**copy.deepcopy(meta), **copy.deepcopy(prior)}
        restored.update(capture_state='preserved_unverified', preserved_existing=True,
                        capture_error='Pre-capture section restored; auxiliary source verification pending')
        omitted.append(restored)
    return omitted


def preview_snapshot(review_ids):
    """Plan in one read-only snapshot; apply rechecks affected rows under lock."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute('''SELECT aptem_review_id FROM "Learner".reviews
            WHERE aptem_review_id=ANY(%s)''', [review_ids])
        preview = {str(row[0]): {'sections': []} for row in cursor.fetchall()}
        cursor.execute('''SELECT r.aptem_review_id,s.section_name FROM "Learner".reviews r
            JOIN "Learner".review_sections s ON s.review_id=r.id
            WHERE r.aptem_review_id=ANY(%s) ORDER BY r.id,s.section_order,s.id''', [review_ids])
        for rid, name in cursor.fetchall():
            preview[str(rid)]['sections'].append({'section_name': name})
    return preview


class Command(BaseCommand):
    help = 'Restore omitted nonempty review sections from an explicit repair backup directory.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--backup-directory', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--workers', type=int, default=5)

    def handle(self, *args, **options):
        directory = Path(options['backup_directory']).resolve()
        if not directory.is_dir():
            raise CommandError('Backup directory does not exist')
        if not 1 <= options['workers'] <= 5:
            raise CommandError('workers must be 1..5')
        counts = Counter()
        candidates = []
        backups = sorted(directory.glob('*.json.gz'))
        preview = preview_snapshot([p.name.removesuffix('.json.gz') for p in backups])
        for backup in backups:
            with gzip.open(backup, 'rt', encoding='utf-8') as stream:
                before = json.load(stream)
            if not before:
                continue
            rid = str(before['review']['aptem_review_id'])
            if backup.name != f'{rid}.json.gz':
                raise CommandError('Backup review ID does not match its filename')
            counts['examined'] += 1
            if rid not in preview:
                raise CommandError(f'Review {rid} no longer exists; nothing restored')
            planned = omitted_sections(before, preview[rid])
            if not planned:
                continue
            if not options['apply']:
                counts['reviews_with_omissions'] += 1
                counts['sections_to_restore'] += len(planned)
                continue
            candidates.append(before)
        if options['apply']:
            shards = [candidates[i::options['workers']] for i in range(options['workers'])]
            with ThreadPoolExecutor(max_workers=options['workers']) as pool:
                for result in pool.map(lambda shard: self.restore_group(shard, directory.name), shards):
                    counts.update(result)
        self.stdout.write(json.dumps({'applied': options['apply'], 'counts': dict(counts)}))

    def restore_group(self, snapshots, backup_run):
        connection.close()
        counts = Counter()
        try:
            for before in snapshots:
                restored = self.restore_one(before, backup_run)
                if restored:
                    counts['reviews_restored'] += 1
                    counts['sections_restored'] += restored
            return counts
        finally:
            connection.close()

    def restore_one(self, before, backup_run):
        rid = str(before['review']['aptem_review_id'])
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SELECT id FROM "Learner".reviews WHERE aptem_review_id=%s FOR UPDATE', [rid])
            if not cursor.fetchone():
                raise CommandError(f'Review {rid} no longer exists; nothing restored')
            current = read_stored(rid)
            for key in ('id', 'learner_id', 'aptem_review_id'):
                if current['review'][key] != before['review'][key]:
                    raise CommandError(f'Review {rid} identity differs; refused restore')
            additions = omitted_sections(before, current)
            if not additions:
                return 0
            data = current['review']['review_data']
            data['sections'] = list(data.get('sections') or []) + additions
            data['capture_verified'] = False
            reason = 'Prior sections restored from backup; auxiliary source verification pending'
            data['content_audit'] = {'verified': False, 'checked_at': now(), 'reason': reason,
                                     'backup_run': backup_run}
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            combined = current['sections'] + additions
            raw = '\n\n'.join(s['section_name']+'\n'+s.get('raw_text', '') for s in combined).strip()
            pk = current['review']['id']
            cursor.execute('SELECT coalesce(max(section_order),0) FROM "Learner".review_sections WHERE review_id=%s', [pk])
            last_order = cursor.fetchone()[0]
            for order, section in enumerate(additions, last_order + 1):
                cursor.execute('''INSERT INTO "Learner".review_sections
                    (review_id,section_name,section_order,fields_json,tables_json,raw_text,created_at,updated_at)
                    VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,now(),now())''',
                    [pk, section['section_name'], order, json.dumps(section['fields'], ensure_ascii=False),
                     json.dumps(section['tables'], ensure_ascii=False), section['raw_text']])
            cursor.execute('''UPDATE "Learner".reviews SET review_data=%s::jsonb,raw_text=%s,
                content_hash=%s,extraction_status='partial',last_error=%s,updated_at=now() WHERE id=%s''',
                [encoded, raw, digest, reason, pk])
            check = read_stored(rid)
            expected = [{key: section[key] for key in ('section_name', 'fields', 'tables', 'raw_text')}
                        for section in combined]
            if check['sections'] != expected or check['review']['review_data'] != data:
                raise CommandError(f'Review {rid} restore readback differed; rolled back')
            return len(additions)
