"""Persistence for the Learner schema with canonical learner identity snapshots."""
import hashlib
import json

from django.db import connection, transaction


def validate_replacement(old_sections, new_sections):
    """Reject a repair that would erase previously captured section content."""
    old_by_name = {}
    for section in old_sections:
        name = section.get('section_name')
        if name in old_by_name and old_by_name[name] != section:
            raise ValueError('Ambiguous previous sections; previous content preserved')
        # The legacy extractor sometimes captured the exact same section twice.
        # Collapse only identical duplicates; never discard distinct answers.
        old_by_name[name] = section
    new_by_name = {}
    for section in new_sections:
        name = section.get('section_name')
        if not name or name in new_by_name:
            raise ValueError('Ambiguous replacement sections; previous content preserved')
        new_by_name[name] = section
    for name, old in old_by_name.items():
        if name not in new_by_name:
            raise ValueError('Section inventory changed; previous content preserved')
        new = new_by_name[name]
        if old.get('raw_text', '').strip() and not new.get('raw_text', '').strip():
            raise ValueError('Repair would erase populated section; previous content preserved')


def destination_learner(aptem_id):
    with connection.cursor() as cursor:
        cursor.execute('''
            SELECT l.id FROM "Learner".learners l
            JOIN "LMS"."Aptem_users" a
              ON lower(trim(l.email)) = lower(trim(a."Email"))
            WHERE a."ID" = %s AND a."Program-Status" = 'Active'
              AND trim(a."Email") <> ''
        ''', [aptem_id])
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("Active Aptem learner must map to exactly one destination learner by email")
    return rows[0][0]


def save_review(payload):
    planned = payload.get('review_category') == 'planned' and payload.get('status') != 'Completed'
    in_progress = payload.get('status') == 'In Progress' and payload.get('capture_version', 0) >= 3
    if payload.get('programme_status') != 'Active' or (payload.get('status') != 'Completed' and not planned and not in_progress):
        raise ValueError('Only live-verified Active learners and Completed reviews may be stored')
    sections = payload.get('sections', [])
    if (not sections and (not planned or in_progress)) or any(not s.get('section_name') for s in sections):
        raise ValueError('Review contains missing sections')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with transaction.atomic():
        learner_id = destination_learner(payload['aptem_learner_id'])
        with connection.cursor() as cursor:
            # Existing tables lack natural-key uniqueness. Serialize writers without
            # adding constraints or allowing a repeated import to create duplicates.
            cursor.execute('LOCK TABLE "Learner".reviews IN SHARE ROW EXCLUSIVE MODE')
            cursor.execute('SELECT id FROM "Learner".reviews WHERE learner_id=%s AND aptem_review_id=%s',
                           [learner_id, payload['aptem_review_id']])
            existing = cursor.fetchall()
            if len(existing) > 1:
                raise ValueError('Duplicate destination reviews require manual reconciliation')
            if existing and payload.get('capture_version', 0) >= 3:
                cursor.execute('SELECT review_data FROM "Learner".reviews WHERE id=%s', [existing[0][0]])
                old_data = cursor.fetchone()[0] or {}
                if isinstance(old_data, str):
                    old_data = json.loads(old_data)
                validate_replacement(old_data.get('sections', []), sections)
            if planned and existing:
                cursor.execute('SELECT status,review_data FROM "Learner".reviews WHERE id=%s', [existing[0][0]])
                stored_status, stored_data = cursor.fetchone()
                if isinstance(stored_data, str):
                    stored_data = json.loads(stored_data)
                if stored_status == 'Completed' or (not sections and (stored_data or {}).get('sections')):
                    raise ValueError('Refusing to replace a Completed review with Planned metadata')
            values = [payload['review_name'], payload['review_type'], payload['reviewer_name'],
                      payload['status'], payload['source_url'], encoded, payload['raw_text'], digest]
            if existing:
                review_pk = existing[0][0]
                cursor.execute('''UPDATE "Learner".reviews SET review_name=%s,review_type=%s,
                    reviewer_name=%s,status=%s,source_url=%s,review_data=%s::jsonb,raw_text=%s,
                    content_hash=%s,extraction_status='success',last_error=NULL,last_seen_at=now(),
                    last_extracted_at=now(),updated_at=now() WHERE id=%s''', values+[review_pk])
                cursor.execute('DELETE FROM "Learner".review_sections WHERE review_id=%s', [review_pk])
            else:
                cursor.execute('''INSERT INTO "Learner".reviews
                    (review_name,review_type,reviewer_name,status,source_url,review_data,raw_text,
                     content_hash,learner_id,aptem_review_id,last_extracted_at)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,now()) RETURNING id''',
                    values+[learner_id,payload['aptem_review_id']])
                review_pk = cursor.fetchone()[0]
            cursor.execute('''UPDATE "Learner".reviews r
                SET learner_name=l.full_name, learner_email=l.email
                FROM "Learner".learners l WHERE r.id=%s AND l.id=r.learner_id''', [review_pk])
            for order, section in enumerate(sections, 1):
                section_fields = list(section['fields'])
                if section.get('links'):
                    section_fields.append({'label': 'Attachments', 'value': '', 'links': section['links']})
                cursor.execute('''INSERT INTO "Learner".review_sections
                    (review_id,section_name,section_order,fields_json,tables_json,raw_text)
                    VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s)''',
                    [review_pk,section['section_name'],order,json.dumps(section_fields),
                     json.dumps(section['tables']),section['raw_text']])
            if planned:
                cursor.execute('''UPDATE "Learner".reviews SET extraction_status='partial',
                    last_error=%s WHERE id=%s''',
                    ['Planned list metadata only; review not started and no detail sections extracted.', review_pk])
    return {'review_id': review_pk, 'learner_id': learner_id, 'sections_saved': len(sections)}
