"""Local, atomic audit checkpoints; never infer completeness from existing rows."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from django.db import connection


def now():
    return datetime.now(timezone.utc).isoformat()


def shard_records(records, worker_count, worker_index):
    if not 1 <= worker_count <= 5 or not 0 <= worker_index < worker_count:
        raise ValueError('Worker count must be 1..5 and index must be 0..count-1')
    return {key: value for key, value in records.items() if int(key) % worker_count == worker_index}


def active_snapshot():
    with connection.cursor() as cursor:
        cursor.execute('''SELECT "ID", "FullName", "Email", "Learner Phone"
            FROM "LMS"."Aptem_users" WHERE "Program-Status"='Active'
            AND "ID" IS NOT NULL ORDER BY "ID"''')
        sources = cursor.fetchall()
        cursor.execute('SELECT id, full_name, email, phone_number FROM "Learner".learners')
        destinations = cursor.fetchall()
        cursor.execute('''SELECT learner_id, count(*),
            count(*) FILTER (WHERE status='Completed' AND extraction_status='success'),
            max(last_extracted_at) FROM "Learner".reviews GROUP BY learner_id''')
        counts = {str(row[0]): row[1:] for row in cursor.fetchall()}
    normalize = lambda value: (value or '').strip().casefold()
    phone = lambda value: ''.join(c for c in (value or '') if c.isdigit())
    source_emails = Counter(normalize(row[2]) for row in sources)
    records = {}
    for aptem_id, name, email, number in sources:
        matches = [d for d in destinations if normalize(email) and normalize(d[2]) == normalize(email)]
        mapped = len(matches) == 1 and source_emails[normalize(email)] == 1
        destination_id = matches[0][0] if mapped else None
        candidates = []
        if not mapped:
            for dest_id, dest_name, dest_email, dest_phone in destinations:
                reasons = []
                if normalize(name) and normalize(name) == normalize(dest_name):
                    reasons.append('exact_name')
                if len(phone(number)) >= 9 and phone(number) == phone(dest_phone):
                    reasons.append('exact_phone')
                if normalize(email) and normalize(email) == normalize(dest_email):
                    reasons.append('exact_email')
                if reasons:
                    candidates.append({'learner_id': dest_id, 'name': dest_name,
                                       'email': dest_email, 'evidence': reasons})
        existing = counts.get(str(destination_id), (0, 0, None))
        key = str(aptem_id)
        if key in records:
            raise ValueError('Duplicate Active Aptem learner ID')
        records[key] = {
            'aptem_learner_id': key, 'name': name, 'email': email,
            'learner_id': destination_id,
            'mapping_status': 'matched' if mapped else 'unresolved',
            'mapping_candidates': candidates,
            'existing_reviews': existing[0], 'existing_completed_reviews': existing[1],
            'last_existing_extraction': str(existing[2]) if existing[2] else None,
            'status': 'pending' if mapped else 'mapping_issue',
            'attempts': 0, 'planned_inventory_complete': False,
            'finished_inventory_complete': False, 'expected_finished': None,
            'saved_review_ids': [], 'saved_completed_ids': [],
            'in_progress_metadata_only_ids': [], 'errors': [],
        }
    return records


def classify(record):
    if record['mapping_status'] != 'matched':
        return 'mapping_issue'
    if record['errors']:
        return 'partial' if record['saved_review_ids'] else 'failed'
    if not (record['planned_inventory_complete'] and record['finished_inventory_complete']):
        return 'partial'
    if record['expected_finished'] != len(record['saved_completed_ids']):
        return 'partial'
    if record['in_progress_metadata_only_ids']:
        return 'partial'
    return 'complete' if record['saved_review_ids'] else 'no_reviews'


def checkpoint(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    report['updated_at'] = now()
    report['summary'] = dict(Counter(r['status'] for r in report['learners'].values()))
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def access_failure(exc):
    if any(marker in str(exc) for marker in (
        'access_denied:', 'authentication_failed:', 'session_expired:',
        'Could not find Aptem login CSRF token',
    )):
        return True
    # Network-level failures against Aptem repeat identically for every learner;
    # treat them as access failures so the audit halts instead of logging 250 identical
    # errors against a host that is clearly unreachable. The watchdog restarts on halt.
    if type(exc).__name__ in ('ConnectionError', 'ConnectTimeout', 'ReadTimeout',
                               'ProtocolError', 'SSLError', 'NewConnectionError'):
        if 'aptem.co.uk' in str(exc):
            return True
    return False
