"""Explicit learner identity and conservative linking of original PDFs."""
import json
import re
from datetime import datetime

from django.db import connection, transaction

TABLE = '"Learner".review_documents'


def as_dict(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def norm(value):
    return ' '.join((value or '').split()).casefold()


def document_type(filename):
    return re.sub(r'\s+\d{4}\.\d{2}\.\d{2}.*$', '', re.sub(r'^Review - ', '', filename, flags=re.I)).removesuffix('.pdf').strip()


def map_learner(aptem_id, users, learners):
    direct = [r for r in learners if str(r['aptem_id']) == str(aptem_id)]
    if len(direct) == 1:
        return direct[0]['id'], 'aptem_id'
    if direct:
        return None, 'ambiguous_aptem_id'
    sources = [r for r in users if str(r['id']) == str(aptem_id)]
    if len(sources) != 1 or not norm(sources[0]['email']):
        return None, 'unresolved'
    email = norm(sources[0]['email'])
    if sum(norm(r['email']) == email for r in users) != 1:
        return None, 'ambiguous_email'
    matches = [r for r in learners if norm(r['email']) == email]
    if len(matches) != 1:
        return None, 'unresolved_email'
    if matches[0]['aptem_id'] is not None and str(matches[0]['aptem_id']) != str(aptem_id):
        return None, 'conflicting_identity'
    return matches[0]['id'], 'unique_email'


def discover(scope='all', learner_ids=None):
    with connection.cursor() as c:
        c.execute('SELECT "ID","Email","Program-Status" FROM "LMS"."Aptem_users"')
        users = [dict(zip(('id','email','status'), r)) for r in c.fetchall()]
        c.execute('SELECT id,aptem_id,email FROM "Learner".learners')
        learners = [dict(zip(('id','aptem_id','email'), r)) for r in c.fetchall()]
        c.execute('''SELECT evidence_id,learner_id,evidence_name,source_file_url,
            source_hash,completed_date,source_synced_at
            FROM fetching_evidence.evidence_items
            WHERE evidence_kind='File' AND evidence_name ILIKE 'Review - %%.pdf'
            ORDER BY learner_id,evidence_id''')
        rows = c.fetchall()
    active = {str(r['id']) for r in users if r['status'] == 'Active'}
    cache = {}
    results = []
    for eid, aptem_id, name, url, digest, completed, synced in rows:
        if scope == 'active' and str(aptem_id) not in active:
            continue
        if learner_ids and str(aptem_id) not in learner_ids:
            continue
        if aptem_id not in cache:
            cache[aptem_id] = map_learner(aptem_id, users, learners)
        learner_id, method = cache[aptem_id]
        results.append({'source_evidence_id':eid, 'aptem_learner_id':aptem_id,
            'learner_id':learner_id, 'mapping_status':method, 'original_filename':name,
            'document_type':document_type(name), 'source_url':url or '', 'source_hash':digest,
            'source_completed_at':completed, 'source_synced_at':synced})
    return results


def seed(rows):
    """Only queue new documents; never reset successful work or concurrent claims."""
    with transaction.atomic(), connection.cursor() as c:
        c.executemany(f'''INSERT INTO {TABLE}
                (source_evidence_id,aptem_learner_id,learner_id,mapping_status,
                 original_filename,document_type,source_url,source_hash,source_completed_at,source_synced_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_evidence_id) DO NOTHING''', [[r[k] for k in (
                    'source_evidence_id','aptem_learner_id','learner_id','mapping_status',
                    'original_filename','document_type','source_url','source_hash',
                    'source_completed_at','source_synced_at')] for r in rows])


def parse_date(value):
    for fmt in ('%d %b %Y','%d/%m/%Y','%Y-%m-%d'):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            pass
    return None


def match_review(document, metadata, reviews):
    """Require same learner, type, and actual PDF completion date; never use upload date."""
    if document['learner_id'] is None:
        return None, 'learner_unresolved'
    completed = metadata.get('completed_date')
    title = norm(metadata.get('title'))
    if not completed or not title:
        return None, 'insufficient_metadata'
    candidates = []
    for r in reviews:
        if r['learner_id'] != document['learner_id'] or r['status'] != 'Completed':
            continue
        data = as_dict(r.get('review_data'))
        if str(data.get('aptem_learner_id')) != str(document['aptem_learner_id']):
            continue
        source = data.get('source_metadata', {})
        if title not in {norm(r['review_name']), norm(r['review_type'])}:
            continue
        if parse_date(source.get('Completed Date')) != completed:
            continue
        planned = metadata.get('planned_date')
        stored_planned = parse_date((source.get('Planned / Scheduled Date') or '').split(' at ')[0])
        if planned and stored_planned and planned != stored_planned:
            continue
        candidates.append(r['id'])
    if len(candidates) == 1:
        return candidates[0], 'matched_pdf_type_dates'
    return None, 'ambiguous_review' if candidates else 'review_not_found'


def reviews_for(learner_id):
    if learner_id is None:
        return []
    with connection.cursor() as c:
        c.execute('''SELECT id,learner_id,review_name,review_type,status,review_data
            FROM "Learner".reviews WHERE learner_id=%s''', [learner_id])
        return [dict(zip(('id','learner_id','review_name','review_type','status','review_data'),r)) for r in c.fetchall()]


def summary():
    with connection.cursor() as c:
        c.execute(f'''SELECT status,count(*),count(DISTINCT aptem_learner_id)
            FROM {TABLE} GROUP BY status''')
        statuses = {r[0]:{'files':r[1],'learners':r[2]} for r in c.fetchall()}
        c.execute(f'''SELECT count(*),count(DISTINCT aptem_learner_id),
            count(*) FILTER (WHERE learner_id IS NULL),
            count(*) FILTER (WHERE status='uploaded' AND review_id IS NOT NULL),
            coalesce(sum(file_size),0),max(uploaded_at) FROM {TABLE}''')
        total, learners, unmapped, linked, size, last = c.fetchone()
    return {'statuses':statuses,'total_files':total,'total_learners':learners,
        'learner_unresolved':unmapped,'uploaded_review_linked':linked,'bytes':size,'last_upload':last}
