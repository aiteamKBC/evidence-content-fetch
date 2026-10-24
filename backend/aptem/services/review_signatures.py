"""Read Aptem's current signature grid; enrich existing reviews only.

Signature enum matches Aptem eSignaturesGrid.js: 0 No, 1 Yes, 2 N/A.
Read endpoints use POST (Kendo); no source mutation endpoints are permitted.
"""
import hashlib
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from django.conf import settings
from django.db import connection, transaction

from .review_api_sync import login_session

KEY = 'signature_status'
ROLES = {'learner': 'Participant', 'coach': 'Advisor',
         'employer': 'Employer', 'referrer': 'Referrer'}
ENDPOINTS = {
    'signatures': '/MWS.eDocuments/Signatures/SignaturesRead',
    'episodes': '/MWS.Review/Review/ReadReviewsEpisodes',
    'groups': '/MWS.eDocuments/DynamicDocuments/ReadReviewDocumentsGroups',
}
TYPES = ('monthly coaching meeting', 'progress review',
         'progress review (+ skills radar)', 'progress review master')


class SignatureError(ValueError):
    pass


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def write_verified(path, value):
    """Durable local recovery/staging file, with corruption detection on reload."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as out:
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    digest = hashlib.sha256(encoded).hexdigest()
    path.with_suffix(path.suffix + '.sha256').write_text(digest, encoding='ascii')
    if load_verified(path) != json.loads(encoded):
        raise SignatureError('Local recovery file readback failed')


def load_verified(path):
    path = Path(path)
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != path.with_suffix(path.suffix + '.sha256').read_text().strip():
        raise SignatureError('Recovery/staging checksum mismatch')
    return json.loads(encoded)


def export_followups(folder, rows):
    """One row per missing role, suitable for review before creating a ticket."""
    path = Path(folder) / 'missing-signatures.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['Learner', 'Aptem learner ID', 'Review ID', 'Review type',
                         'Missing role', 'Follow-up contact', 'Contact Aptem ID',
                         'Contact basis', 'Checked at', 'Review link'])
        for row in rows:
            status = row['after']
            if status['state'] != 'missing' or status['verified'] is not True:
                continue
            for role in status['missing_roles']:
                contact = status['roles'][role]['follow_up_contact']
                values = [row['learner_name'], row['aptem_learner_id'], row['aptem_review_id'],
                    row['review_type'], role, contact['name'], contact['aptem_id'], contact['basis'],
                    status['checked_at'], settings.APTEM_BASE_URL.rstrip('/') +
                    '/MWS.Review/Review/Get/' + row['aptem_review_id']]
                # Names originate outside this app; avoid spreadsheet formula execution.
                writer.writerow(["'" + str(v) if str(v or '').startswith(('=', '+', '-', '@')) else v for v in values])
    return path


class SignatureClient:
    def __init__(self, session=None):
        self.session = session or login_session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0',
                                     'X-Requested-With': 'XMLHttpRequest',
                                     'Accept': 'application/json'})

    def close(self):
        self.session.close()

    def page(self, endpoint, values):
        path = ENDPOINTS[endpoint]
        for attempt in range(3):
            try:
                response = self.session.post(settings.APTEM_BASE_URL.rstrip('/') + path,
                    files={k: (None, str(v)) for k, v in values.items()},
                    timeout=(15, 60), allow_redirects=False)
                if response.status_code in (302, 401, 403):
                    self.close()
                    self.__init__()
                    continue
                if response.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code != 200:
                    raise SignatureError(f'{endpoint}: HTTP {response.status_code}')
                data = response.json()
                if (not isinstance(data, dict) or not isinstance(data.get('Data'), list)
                        or type(data.get('Total')) is not int or data['Total'] < 0
                        or data.get('Errors') not in (None, [], {})):
                    raise SignatureError(f'{endpoint}: invalid grid response')
                return data
            except (requests.RequestException, json.JSONDecodeError):
                time.sleep(2 ** attempt)
        raise SignatureError(f'{endpoint}: read failed after retries')

    def rows(self, endpoint, identity, **params):
        result, seen, expected = [], set(), None
        for _ in range(1000):
            page = self.page(endpoint, {'filter': '', 'group': '',
                'sort': identity + '-asc', 'take': 200, 'skip': len(result), **params})
            if expected is None:
                expected = page['Total']
            if page['Total'] != expected:
                raise SignatureError(f'{endpoint}: inventory changed while paging')
            for row in page['Data']:
                key = row.get(identity)
                if key is None or key in seen:
                    raise SignatureError(f'{endpoint}: duplicate/missing grid identity')
                seen.add(key)
                result.append(row)
            if len(result) == expected:
                return result
            if not page['Data'] or len(result) > expected:
                raise SignatureError(f'{endpoint}: incomplete pagination')
        raise SignatureError(f'{endpoint}: excessive pagination')

    def learner(self, aptem_id):
        aptem_id = int(aptem_id)
        signatures = self.rows('signatures', 'DocumentId', filter=f'EntityId~eq~{aptem_id}')
        if any(row.get('EntityId') != aptem_id for row in signatures):
            raise SignatureError('Signature response learner identity mismatch')
        episodes = self.rows('episodes', 'ProgramId', userId=aptem_id)
        if any(row.get('UserId') != aptem_id for row in episodes):
            raise SignatureError('Programme response learner identity mismatch')
        groups = []
        for episode in episodes:
            # Aptem GroupKey is programme + title + date; distinct reviews can share it.
            rows = self.rows('groups', 'ItemId', userId=aptem_id, programId=episode['ProgramId'])
            if any(row.get('ProgramId') != episode['ProgramId'] for row in rows):
                raise SignatureError('Document group programme identity mismatch')
            groups.extend(rows)
        return {'aptem_learner_id': str(aptem_id),
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'signatures': signatures, 'groups': groups}


def classify(review_id, aptem_id, evidence):
    """Never convert absent documents, unsupported enums or conflicts into missing."""
    if str(aptem_id) != evidence['aptem_learner_id']:
        raise SignatureError('Evidence learner identity mismatch')
    result = {'version': 1, 'source': 'aptem_classic_signature_grid',
        'checked_at': evidence['checked_at'], 'aptem_review_id': str(review_id),
        'aptem_learner_id': str(aptem_id), 'state': 'unknown', 'verified': False,
        'missing_roles': [], 'signed_roles': [], 'roles': {
            role: {'status': 'unknown', 'required': None} for role in ROLES}}
    if evidence.get('error'):
        result['reason'] = evidence['error']
        return result
    groups = [g for g in evidence['groups'] if str(g.get('ItemId')) == str(review_id) and g.get('ItemType') == 9]
    docs = [d for d in evidence['signatures'] if str(d.get('ItemId')) == str(review_id) and d.get('ItemType') == 9]
    if len(groups) != 1 or len(docs) > 1:
        result['reason'] = 'source_review_not_visible' if not groups else 'ambiguous_source_documents'
        return result
    group = groups[0]
    result['document_count'] = group.get('DocCount')
    if group.get('DocCount') == 0 and not docs:
        result.update(state='document_not_generated', verified=True, reason='no_current_document')
        return result
    if not docs or not isinstance(group.get('DocCount'), int) or group['DocCount'] <= 0:
        result['reason'] = 'current_document_unavailable'
        return result
    doc = docs[0]
    if doc.get('EntityId') != int(aptem_id):
        raise SignatureError('Document learner identity mismatch')
    result.update(document_id=doc['DocumentId'], document_created_at=doc.get('CreateDateTime'),
                  document_program_id=doc.get('ProgramId'), group_program_id=group.get('ProgramId'))
    contacts = {
        'learner': (doc['EntityId'], doc.get('EntityName'), 'learner'),
        'coach': (doc.get('OwnerId'), doc.get('OwnerName'), 'current_review_owner'),
        'employer': (doc.get('ManagerId'), doc.get('ManagerName'), 'current_line_manager'),
        'referrer': (doc.get('ReferrerId'), None, 'current_referrer'),
    }
    roles = {}
    for role, prefix in ROLES.items():
        code = doc.get(prefix + 'Signature')
        signer_id = doc.get('ParticipantId' if role == 'learner' else prefix + 'SignerId')
        group_id = group.get(prefix + 'SignerId')
        if type(code) is not int or code not in (0, 1, 2):
            result['reason'] = 'unsupported_signature_enum'
            return result
        if ((code == 1 and (not signer_id or signer_id != group_id))
                or (code != 1 and (signer_id is not None or group_id is not None))):
            result['reason'] = 'signature_sources_disagree'
            return result
        contact_id, contact_name, basis = contacts[role]
        roles[role] = {'status': {0: 'missing', 1: 'signed', 2: 'not_required'}[code],
            'required': code != 2, 'source_status': code,
            'signed_by_id': signer_id, 'signed_by_name': doc.get(prefix + 'SignerName'),
            'signed_at': doc.get(prefix + 'SignerDate'),
            'has_mandate': doc.get(prefix + 'HasMandate'),
            'follow_up_contact': {'aptem_id': contact_id, 'name': contact_name, 'basis': basis}}
    missing = [r for r, v in roles.items() if v['status'] == 'missing']
    signed = [r for r, v in roles.items() if v['status'] == 'signed']
    if (missing and group.get('IsFullySigned') is True) or (not missing and group.get('IsFullySigned') is False):
        result['reason'] = 'signature_sources_disagree'
        return result
    result.update(roles=roles, verified=True, missing_roles=missing, signed_roles=signed,
                  state='missing' if missing else ('signed' if signed else 'not_required'))
    return result


def inventory(learner_id=None):
    with connection.cursor() as cursor:
        cursor.execute('''SELECT r.id,r.learner_id,r.aptem_review_id,l.aptem_id,
            l.full_name,r.review_type,coalesce(r.review_data ? %s,false),
            r.review_data->%s,r.review_data IS NULL,
            r.review_data->>'aptem_learner_id',jsonb_typeof(r.review_data)
            FROM "Learner".reviews r JOIN "Learner".learners l ON l.id=r.learner_id
            WHERE lower(trim(r.review_type)) = ANY(%s)
            AND coalesce(r.review_data->>'synthetic_lms_validation','false') <> 'true'
            AND (%s::text IS NULL OR l.aptem_id::text=%s) ORDER BY r.id''',
            [KEY, KEY, list(TYPES), learner_id, learner_id])
        result = []
        for pk, canonical, review, aptem, name, kind, present, old, was_null, stored_learner, shape in cursor.fetchall():
            if not str(review).isdigit() or not str(aptem).isdigit():
                raise SignatureError('Non-numeric source identity in scoped reviews')
            if shape not in (None, 'object') or (stored_learner and str(stored_learner) != str(aptem)):
                raise SignatureError(f'Stored metadata identity/shape mismatch at row {pk}')
            result.append(dict(id=pk, learner_id=canonical, aptem_review_id=str(review),
                aptem_learner_id=str(aptem), learner_name=name, review_type=kind,
                before_present=present, before=decode(old), before_null=was_null))
    identities = defaultdict(set)
    for row in result:
        identities[row['aptem_learner_id']].add(row['learner_id'])
    if any(len(v) != 1 for v in identities.values()):
        raise SignatureError('Aptem learner maps to multiple canonical learners')
    return result


def fingerprint():
    """Server hashes protect all existing content while avoiding content downloads."""
    result = {}
    with connection.cursor() as cursor:
        for table, expression in [
            ('reviews', "(to_jsonb(t)-'review_data') || jsonb_build_object('review_data', coalesce(review_data,'{}'::jsonb)-'signature_status')"),
            ('review_sections', 'to_jsonb(t)')]:
            cursor.execute(f'''SELECT count(*),md5(string_agg(md5(({expression})::text),'' ORDER BY id))
                FROM "Learner".{table} t''')
            result[table] = list(cursor.fetchone())
    return result


def apply_rows(rows, restore=False):
    """Compare-and-set only our JSON key. All-or-nothing for each small batch.

    Callers must have written and verified the full recovery file beforehand.
    Reapplying and restoring are resumable; conflicting later changes abort.
    """
    changed = 0
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout='3s'")
        cursor.execute("SET LOCAL statement_timeout='30s'")
        cursor.execute('''SELECT r.id,r.learner_id,r.aptem_review_id,l.aptem_id,
            coalesce(r.review_data ? %s,false),r.review_data->%s,jsonb_typeof(r.review_data)
            FROM "Learner".reviews r JOIN "Learner".learners l ON l.id=r.learner_id
            WHERE r.id=ANY(%s) ORDER BY r.id FOR UPDATE OF r''',
            [KEY, KEY, [r['id'] for r in rows]])
        current_rows = {r[0]: r[1:] for r in cursor.fetchall()}
        changes = []
        for row in rows:
            current = current_rows.get(row['id'])
            if not current or (current[0], str(current[1]), str(current[2])) != (
                    row['learner_id'], row['aptem_review_id'], row['aptem_learner_id']):
                raise SignatureError(f'Destination identity changed at row {row["id"]}')
            if current[5] not in (None, 'object'):
                raise SignatureError('Destination metadata is not an object')
            value = decode(current[4])
            target_present, target = (row['before_present'], row['before']) if restore else (True, row['after'])
            expected_present, expected = (True, row['after']) if restore else (row['before_present'], row['before'])
            if current[3] == target_present and value == target:
                continue
            if current[3] != expected_present or value != expected:
                raise SignatureError(f'Concurrent signature metadata change at row {row["id"]}')
            changes.append(dict(id=row['id'], present=target_present, value=target, was_null=row['before_null']))
        if changes:
            cursor.execute('''UPDATE "Learner".reviews r SET review_data=CASE WHEN x.present THEN
                jsonb_set(coalesce(r.review_data,'{}'::jsonb),ARRAY[%s],coalesce(x.value,'null'::jsonb),true)
                WHEN x.was_null AND r.review_data-%s='{}'::jsonb THEN NULL
                ELSE r.review_data-%s END
                FROM jsonb_to_recordset(%s::jsonb) AS x(id bigint,present boolean,value jsonb,was_null boolean)
                WHERE r.id=x.id RETURNING r.id,coalesce(r.review_data ? %s,false),r.review_data->%s''',
                [KEY, KEY, KEY, json.dumps(changes), KEY, KEY])
            saved = {pk: (present, decode(value)) for pk, present, value in cursor.fetchall()}
            if any(saved.get(r['id']) != (r['present'], r['value']) for r in changes):
                raise SignatureError('Signature metadata readback failed')
            changed = len(changes)
    return changed
