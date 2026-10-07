"""Read-only, independent checks of live review identities and stored captures."""
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.services.review_api_sync import FLAT_LABELS, live_inventory, login_session


CONTENT_STATUSES = {'Completed', 'InProgress', 'AwaitingSignature', 'Archived'}
VERIFIED_STATES = {'verified', 'verified_source_absent'}
SECTION_SNAPSHOT_SQL = '''WITH captured_reviews AS MATERIALIZED (
    SELECT id,review_data->'sections' AS captures FROM "Learner".reviews
    WHERE id=ANY(%s)
), captured_sections AS MATERIALIZED (
    SELECT r.id,item.ordinality AS section_order,item.value AS captured
    FROM captured_reviews r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(r.captures)='array' THEN r.captures ELSE '[]'::jsonb END
    ) WITH ORDINALITY item(value,ordinality)
)
SELECT s.review_id,s.section_order,s.section_name,
    c.captured->>'capture_state',c.captured->>'source_action',
    c.captured ? 'source_payload',
    s.section_name IS NOT DISTINCT FROM (c.captured->>'section_name'),
    s.fields_json IS NOT DISTINCT FROM (c.captured->'fields'),
    s.tables_json IS NOT DISTINCT FROM (c.captured->'tables'),
    s.raw_text IS NOT DISTINCT FROM (c.captured->>'raw_text'),
    CASE WHEN jsonb_typeof(c.captured->'source_payload'->'fields')='array'
         THEN c.captured->'source_payload' END,
    CASE WHEN jsonb_typeof(c.captured->'source_payload'->'fields')='array'
         THEN s.fields_json END,
    CASE WHEN c.captured->>'capture_state'='verified_source_absent'
         THEN c.captured->'source_payload' END,
    c.captured->>'source_kind',
    CASE WHEN c.captured->>'source_action'='PLRContent' THEN jsonb_build_object(
        'source_contract',c.captured->'source_contract',
        'source_scope',c.captured->'source_scope',
        'source_learner_id',c.captured->'source_learner_id',
        'source_endpoints',c.captured->'source_endpoints',
        'source_payload',c.captured->'source_payload') END
FROM "Learner".review_sections s
LEFT JOIN captured_sections c ON c.id=s.review_id AND c.section_order=s.section_order
WHERE s.review_id=ANY(%s)
ORDER BY s.review_id,s.section_order,s.id'''


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def same_timestamp(left, right):
    if not left or not right:
        return left == right
    try:
        def timestamp(value):
            result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
        return timestamp(left) == timestamp(right)
    except (ValueError, TypeError):
        return False


def plr_source_contract_valid(entry, learner_id):
    """Check the UI-proven learner PLR source, never the PriorLearning route.

    This deliberately does not call the capture parser or its verification
    helpers: a perfectly mirrored response from the wrong endpoint is invalid.
    The ULN may be null and qualifications may be empty, but all three source
    responses must exist and establish the same exact learner identity.
    """
    capture = entry.get('plr_capture')
    if (entry.get('capture_state') != 'verified'
            or entry.get('source_action') != 'PLRContent'
            or entry.get('has_source_payload') is not True
            or not isinstance(capture, dict)):
        return False
    learner_id = str(learner_id)
    expected_endpoints = {
        'learner_identity': '/odata/1.0/Users',
        'qualifications': f'/api/Users/{learner_id}/PLR',
        'prior_attainment': f'/api/Users/{learner_id}/PLR/PriorAttainmentLevel',
    }
    if (capture.get('source_contract') != 'plr-ui-v1'
            or capture.get('source_scope') != 'current_learner_plr'
            or str(capture.get('source_learner_id')) != learner_id
            or capture.get('source_endpoints') != expected_endpoints):
        return False
    payload = capture.get('source_payload')
    if not isinstance(payload, dict) or any(str(key).endswith('_error') for key in payload):
        return False
    identity = payload.get('learner_identity')
    identity_rows = identity.get('value') if isinstance(identity, dict) else None
    if (not isinstance(identity_rows, list) or len(identity_rows) != 1
            or not isinstance(identity_rows[0], dict)
            or str(identity_rows[0].get('Id')) != learner_id
            or 'UserILRSummary_ULN' not in identity_rows[0]):
        return False
    qualifications = payload.get('qualifications')
    if (not isinstance(qualifications, list)
            or any(not isinstance(row, dict) or str(row.get('userId')) != learner_id
                   for row in qualifications)):
        return False
    attainment = payload.get('prior_attainment')
    required = {'englishLevel', 'mathsLevel', 'ictLevel', 'selectedHighestLevel',
                'learnerSelectedHighestLevel', 'isIlrUserFieldsReadonly'}
    return (isinstance(attainment, dict)
            and str(attainment.get('userId')) == learner_id
            and required <= attainment.keys())


def declared_answer_audit(payload, rendered_fields):
    """Compare actual response values, independently of the capture parser.

    Reuse only the static display-label vocabulary, never the capture parser.
    Resolve title/display collisions independently and compare ordered label/value
    pairs, so swapped answers cannot pass merely because values are unchanged.
    The schema's listAnswers is never used as a response value.
    """
    counters = Counter()
    definitions = payload.get('fields') if isinstance(payload, dict) else None
    if not isinstance(definitions, list) or not definitions:
        return [], counters
    expected, labels = [], Counter()
    title_labels, display_labels = set(), set()

    def unused_label(base, occupied):
        base = str(base or 'Answer')
        result, occurrence = base, 1
        while result in occupied:
            occurrence += 1
            result = f'{base} [{occurrence}]'
        occupied.add(result)
        return result
    statuses = {1: 'Not Started', 2: 'Withdrawn', 3: 'Active',
                4: 'Completed', 5: 'On Hold', 6: 'Paused'}
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        counters['declared_fields'] += 1
        value = payload.get(definition.get('name'))
        if value is None or value == '' or value == [] or value == {}:
            counters['empty_values'] += 1
            if definition.get('listAnswers'):
                counters['empty_values_with_options'] += 1
            continue
        counters['direct_answers'] += 1
        if value is False:
            counters['false_answers'] += 1
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            counters['zero_answers'] += 1
        label = definition.get('title') or definition.get('name') or ''
        labels[str(label)] += 1
        unique_title = unused_label(label, title_labels)
        display_label = unused_label(FLAT_LABELS.get(unique_title, unique_title), display_labels)
        if unique_title == 'programStatus' and isinstance(value, int) and not isinstance(value, bool):
            value = statuses.get(value, value)
        if isinstance(value, bool):
            value = 'Yes' if value else 'No'
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            value = str(value)
        expected.append((display_label, value))
    counters['duplicate_answer_labels'] = sum(count - 1 for count in labels.values())
    if not isinstance(rendered_fields, list) or any(
            not isinstance(field, dict) or not isinstance(field.get('value'), str)
            for field in rendered_fields):
        return ['invalid_rendered_fields'], counters
    expected_counts = Counter(expected)
    actual_pairs = [(field.get('label'), field['value']) for field in rendered_fields]
    actual_counts = Counter(actual_pairs)
    missing = sum((expected_counts - actual_counts).values())
    extra = sum((actual_counts - expected_counts).values())
    counters.update(missing_direct_answers=missing, extra_rendered_answers=extra)
    issues = []
    if missing:
        issues.append('missing_direct_answers')
    if extra:
        issues.append('extra_rendered_answers')
    if not missing and not extra and expected != actual_pairs:
        issues.append('rendered_answer_order_mismatch')
    if len({str(field.get('label')) for field in rendered_fields}) != len(rendered_fields):
        issues.append('duplicate_rendered_labels')
    return issues, counters


def _review_id_filter(values):
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ValueError('Selected audit requires 1..200 positive numeric review IDs')
    selected = {str(value) for value in values}
    if (not selected or len(selected) > 200
            or any(not re.fullmatch(r'[1-9][0-9]*', value) for value in selected)):
        raise ValueError('Selected audit requires 1..200 positive numeric review IDs')
    return sorted(selected, key=int)


def read_snapshot(*, section_batch_size=40, aptem_review_ids=None):
    """One consistent PostgreSQL read-only snapshot; no write-capable helpers."""
    if not 1 <= section_batch_size <= 200:
        raise ValueError('section_batch_size must be 1..200')
    selected = _review_id_filter(aptem_review_ids)
    connection.close()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cursor.execute('''SELECT id,aptem_id FROM "Learner".learners''')
        learners = dict(cursor.fetchall())
        if selected is None:
            cursor.execute('SELECT id FROM "Learner".reviews ORDER BY id')
        else:
            cursor.execute('''SELECT id FROM "Learner".reviews
                WHERE aptem_review_id=ANY(%s) ORDER BY id''', [selected])
        review_ids = [row[0] for row in cursor.fetchall()]
        reviews = []
        for offset in range(0, len(review_ids), 200):
            cursor.execute('''SELECT id,aptem_review_id,learner_id,status,extraction_status,
            jsonb_build_object(
                'capture_version',review_data->'capture_version',
                'content_capture_version',review_data->'content_capture_version',
                'capture_verified',review_data->'capture_verified',
                'content_audit',review_data->'content_audit',
                'content_source_updated_at',review_data->'content_source_updated_at',
                'live_odata',review_data->'live_odata',
                'aptem_review_id',review_data->'aptem_review_id',
                'aptem_learner_id',review_data->'aptem_learner_id',
                'api_meta',jsonb_build_object(
                    'id',review_data->'api_meta'->'id',
                    'userId',review_data->'api_meta'->'userId',
                    'sections',review_data->'api_meta'->'sections',
                    'supplemental_sections',review_data->'api_meta'->'supplemental_sections',
                    'capture_verified',review_data->'api_meta'->'capture_verified',
                    'manifest_reconstructed',review_data->'api_meta'->'manifest_reconstructed')),
            CASE WHEN jsonb_typeof(review_data->'sections')='array'
                 THEN jsonb_array_length(review_data->'sections') ELSE 0 END
            FROM "Learner".reviews WHERE id=ANY(%s) ORDER BY id''',
                           [review_ids[offset:offset + 200]])
            reviews.extend(dict(zip(('id', 'aptem_review_id', 'learner_id', 'status',
                                     'extraction_status', 'data', 'json_section_count'), row))
                           for row in cursor.fetchall())
        for row in reviews:
            row['data'] = decoded(row['data'])
        # Expand each large parent JSON once, rather than repeatedly detoasting
        # it for every projected column of every section. Bounded statements
        # avoid the database statement timeout while keeping the same snapshot.
        # Start from physical rows, including reviews with missing capture JSON.
        if selected is None:
            cursor.execute('SELECT DISTINCT review_id FROM "Learner".review_sections ORDER BY review_id')
        else:
            cursor.execute('''SELECT DISTINCT review_id FROM "Learner".review_sections
                WHERE review_id=ANY(%s) ORDER BY review_id''', [review_ids])
        section_review_ids = [row[0] for row in cursor.fetchall()]
        sections = defaultdict(list)
        for offset in range(0, len(section_review_ids), section_batch_size):
            batch = section_review_ids[offset:offset + section_batch_size]
            cursor.execute(SECTION_SNAPSHOT_SQL, [batch, batch])
            for row in cursor.fetchall():
                entry = dict(zip(('review_id', 'order', 'name', 'capture_state',
                                  'source_action', 'has_source_payload', 'name_matches',
                                  'fields_match', 'tables_match', 'text_match',
                                  'custom_payload', 'custom_fields', 'absence_evidence', 'source_kind',
                                  'plr_capture'), row))
                entry['custom_payload'] = decoded(entry['custom_payload'])
                entry['custom_fields'] = decoded(entry['custom_fields'])
                entry['absence_evidence'] = decoded(entry['absence_evidence'])
                entry['plr_capture'] = decoded(entry.get('plr_capture'))
                sections[entry['review_id']].append(entry)
        orphan_query = '''SELECT DISTINCT s.review_id FROM "Learner".review_sections s
            LEFT JOIN "Learner".reviews r ON r.id=s.review_id WHERE r.id IS NULL'''
        if selected is None:
            cursor.execute(orphan_query)
        else:
            # An orphan has no parent Aptem ID with which to establish selected
            # membership. Do not report unrelated global orphans as scoped gaps.
            cursor.execute(orphan_query + ' AND s.review_id=ANY(%s)', [review_ids])
        orphan_ids = [row[0] for row in cursor.fetchall()]
    return learners, reviews, sections, orphan_ids


def audit_inventory(live, learners, reviews, sections, orphan_ids=(), *, minimum_capture_version=6):
    issues, section_issues = defaultdict(set), []
    totals, answer_totals = Counter(), Counter()
    by_id = defaultdict(list)
    for row in reviews:
        if row['aptem_review_id'] is None:
            issues['stored_without_aptem_id'].add(str(row['id']))
        else:
            by_id[str(row['aptem_review_id'])].append(row)
    for review_id, rows in by_id.items():
        if len(rows) != 1:
            issues['duplicate_aptem_review_id'].add(review_id)
    learner_ids = Counter(str(value) for value in learners.values() if value is not None)
    for learner_id, count in learner_ids.items():
        if count > 1:
            issues['duplicate_canonical_aptem_learner_id'].add(learner_id)
    current_ids = {str(row['Id']) for row in live}
    extras = []
    for review_id in sorted(set(by_id) - current_ids):
        extras.append({'review_id': review_id, 'archived': all(
            row['status'] == 'Archived' for row in by_id[review_id])})
    totals.update(live_reviews=len(live), stored_reviews=len(reviews),
                  historical_extras=len(extras), orphan_review_ids=len(orphan_ids),
                  verified_current_content_reviews=0,
                  current_source_verified_with_unverified_history_reviews=0,
                  unverified_current_source_reviews=0)
    for source in live:
        review_id = str(source['Id'])
        content_scope = source.get('Status') in CONTENT_STATUSES
        totals['content_scope_reviews' if content_scope else 'metadata_scope_reviews'] += 1
        matches = by_id.get(review_id, [])
        if not matches:
            issues['missing_review'].add(review_id)
            if content_scope:
                totals['unverified_current_source_reviews'] += 1
            continue
        if len(matches) != 1:
            if content_scope:
                totals['unverified_current_source_reviews'] += 1
            continue
        stored = matches[0]
        data = stored['data'] or {}
        errors = set()
        if str(learners.get(stored['learner_id'])) != str(source.get('LearnerId')):
            errors.add('canonical_learner_mismatch')
        expected_status = {'NotScheduled': 'Not Scheduled',
                           'InProgress': 'In Progress'}.get(source.get('Status'), source.get('Status'))
        if stored['status'] != expected_status:
            errors.add('status_mismatch')
        old_live = data.get('live_odata') or {}
        if not isinstance(old_live, dict):
            old_live = {}
        if (str(old_live.get('Id')) != review_id
                or str(old_live.get('LearnerId')) != str(source.get('LearnerId'))
                or str(data.get('aptem_review_id')) != review_id
                or str(data.get('aptem_learner_id')) != str(source.get('LearnerId'))):
            errors.add('stored_source_identity_mismatch')
        if not same_timestamp(old_live.get('UpdatedDate'), source.get('UpdatedDate')):
            errors.add('metadata_source_updated_date_mismatch')
        if not content_scope:
            for error in errors:
                issues[error].add(review_id)
            continue
        if stored['extraction_status'] != 'success' or data.get('capture_verified') is not True:
            errors.add('content_unverified')
        version = data.get('content_capture_version') or data.get('capture_version')
        if not str(version).isdigit() or int(version) < minimum_capture_version:
            errors.add('old_content_capture_version')
        if not same_timestamp(data.get('content_source_updated_at'), source.get('UpdatedDate')):
            errors.add('content_source_updated_date_mismatch')
        meta = data.get('api_meta') or {}
        manifest = meta.get('sections')
        supplemental = meta.get('supplemental_sections')
        if not isinstance(supplemental, list):
            errors.add('supplemental_source_inventory_unverified')
            supplemental = []
        expected_sections = []
        if isinstance(manifest, list):
            for section in manifest:
                if not isinstance(section, dict):
                    errors.add('invalid_manifest_section')
                    expected_sections.append({})
                    continue
                expected_sections.append({
                    'name': (section.get('customName') or section.get('name') or '').strip(),
                    'action': (section.get('contentRoute') or {}).get('action'),
                    'supplemental': False})
        for section in supplemental:
            if not isinstance(section, dict):
                errors.add('invalid_supplemental_descriptor')
                expected_sections.append({})
                continue
            expected_sections.append({'name': section.get('section_name'),
                                      'action': section.get('source_action'),
                                      'supplemental': True})
        if str(meta.get('id')) != review_id or str(meta.get('userId')) != str(source.get('LearnerId')):
            errors.add('manifest_identity_mismatch')
        if meta.get('capture_verified') is not True or meta.get('manifest_reconstructed'):
            errors.add('manifest_unverified')
        stored_sections = sections.get(stored['id'], [])
        if (not isinstance(manifest, list) or not manifest
                or len(stored_sections) != stored['json_section_count']
                or len(expected_sections) > len(stored_sections)):
            errors.add('section_cardinality_mismatch')
        if len(stored_sections) > len(expected_sections):
            extras_for_review = stored_sections[len(expected_sections):]
            if all(entry.get('capture_state') == 'preserved_unverified'
                   or entry.get('source_kind') == 'historical_preserved'
                   for entry in extras_for_review):
                errors.add('preserved_extra_sections_unverified')
            else:
                errors.add('extra_sections_unverified')
        if [entry['order'] for entry in stored_sections] != list(range(1, len(stored_sections) + 1)):
            errors.add('section_order_mismatch')
        # A missing row is just as invalid as a correctly mirrored payload from
        # the former (wrong) PriorLearning endpoint. Keep exact repair IDs even
        # when no physical PLR section is available for the loop below.
        by_order = {entry['order']: entry for entry in stored_sections}
        for index, expected in enumerate(expected_sections, 1):
            if (expected.get('action') == 'PLRContent'
                    and not plr_source_contract_valid(by_order.get(index, {}), source.get('LearnerId'))):
                errors.add('plr_source_contract_invalid')
        # Classification is additional reporting only. Preserve every existing
        # error and complete=False while distinguishing unverified history from
        # an actual gap in the current base/supplemental source capture.
        header_errors = set(errors)
        current_source_section_errors = set()
        for entry in stored_sections:
            details = []
            if entry['capture_state'] not in VERIFIED_STATES:
                details.append('section_source_unverified')
            if entry['capture_state'] == 'verified_source_absent':
                evidence = entry.get('absence_evidence') or {}
                evidence = evidence if isinstance(evidence, dict) else {}
                response = evidence.get('meeting_response') or {}
                extended = evidence.get('extended') or {}
                response = response if isinstance(response, dict) else {}
                extended = extended if isinstance(extended, dict) else {}
                if (entry['source_action'] != 'MeetingDetailsContent'
                        or response.get('message') != 'There is no corresponding Review meeting'
                        or str(extended.get('id')) != review_id
                        or str(extended.get('userId')) != str(source.get('LearnerId'))):
                    details.append('source_absence_evidence_invalid')
                else:
                    totals['source_confirmed_absent_meetings'] += 1
            if not entry['has_source_payload']:
                details.append('section_source_payload_missing')
            for key in ('name', 'fields', 'tables', 'text'):
                if not entry.get(key + ('_matches' if key == 'name' else '_match')):
                    details.append('section_' + key + '_json_table_mismatch')
            index = entry['order'] - 1 if isinstance(entry['order'], int) else -1
            if 0 <= index < len(expected_sections):
                expected = expected_sections[index]
                if expected.get('name') != entry['name']:
                    details.append('manifest_section_name_mismatch')
                if expected.get('action') != entry['source_action']:
                    details.append('manifest_section_action_mismatch')
                if expected.get('supplemental') and entry.get('source_kind') != 'supplemental':
                    details.append('supplemental_section_kind_mismatch')
                if (expected.get('action') == 'PLRContent'
                        and not plr_source_contract_valid(entry, source.get('LearnerId'))):
                    details.append('plr_source_contract_invalid')
            answer_errors, counts = declared_answer_audit(entry['custom_payload'], entry['custom_fields'])
            answer_totals.update(counts)
            details.extend(answer_errors)
            if 0 <= index < len(expected_sections):
                current_source_section_errors.update(details)
            totals['content_sections_checked'] += 1
            if details:
                errors.update(details)
                section_issues.append({'review_id': review_id, 'section_order': entry['order'],
                                       'issues': sorted(set(details))})
        if not errors:
            totals['verified_current_content_reviews'] += 1
        else:
            retained = stored_sections[len(expected_sections):]
            source_audit = data.get('content_audit') or {}
            independent_source_failure = (
                isinstance(source_audit, dict) and source_audit.get('verified') is False
                and source_audit.get('reason') !=
                'Prior sections restored from backup; auxiliary source verification pending')
            known_retained_history = bool(retained) and all(
                entry.get('capture_state') == 'preserved_unverified'
                and entry.get('source_kind') == 'historical_preserved'
                and all(entry.get(key) is True for key in
                        ('name_matches', 'fields_match', 'tables_match', 'text_match'))
                for entry in retained)
            source_verified_history_unverified = (
                data.get('capture_verified') is False
                and stored['extraction_status'] == 'partial'
                and bool(source.get('UpdatedDate'))
                and learner_ids[str(source.get('LearnerId'))] == 1
                and bool(expected_sections)
                and header_errors <= {'content_unverified', 'preserved_extra_sections_unverified'}
                and not current_source_section_errors
                and not independent_source_failure
                and known_retained_history)
            totals['current_source_verified_with_unverified_history_reviews'
                   if source_verified_history_unverified else 'unverified_current_source_reviews'] += 1
        for error in errors:
            issues[error].add(review_id)
    review_issues = [values for key, values in issues.items()
                     if key != 'duplicate_canonical_aptem_learner_id']
    totals['reviews_with_issues'] = len(set().union(*review_issues)) if review_issues else 0
    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'complete': not issues and not orphan_ids,
            'totals': dict(totals), 'declared_answer_checks': dict(answer_totals),
            'issue_counts': {key: len(value) for key, value in sorted(issues.items())},
            'issues': {key: sorted(value) for key, value in sorted(issues.items())},
            'section_issues': section_issues, 'historical_extras': extras,
            'orphan_section_review_ids': list(orphan_ids),
            'scope': {'content_statuses': sorted(CONTENT_STATUSES),
                      'minimum_capture_version': minimum_capture_version,
                      'content_totals': 'Three exclusive buckets: no-issue verified captures; exact verified current source plus intact unverified retained history; all remaining missing, unavailable, stale, or otherwise unverified current captures. Retained-history counts do not change complete or issue reporting.',
                      'section_inventory': 'Base manifest plus explicitly recorded supplemental UI sections; original manifest cardinality alone is insufficient.',
                      'metadata_only_statuses_do_not_prove_empty_content': True,
                      'built_in_sections': 'Source retention and JSON/table consistency; custom declared answers and the learner-scoped PLR source contract independently checked.',
                      'source_freshness': 'Compared to live inventory UpdatedDate, not a fresh fetch of every section endpoint.'}}


class Command(BaseCommand):
    help = 'Read-only independent audit of live review identity and corrected content captures.'

    def add_arguments(self, parser):
        parser.add_argument('--report', help='Optional local JSON report path; contains IDs/counts only.')
        parser.add_argument('--minimum-capture-version', type=int, default=6)
        parser.add_argument('--review-ids', help='Optional comma-separated list of 1..200 positive review IDs; audit only these reviews.')

    def handle(self, *args, **options):
        raw_ids = options.get('review_ids')
        try:
            selected = _review_id_filter(
                [value.strip() for value in raw_ids.split(',')] if raw_ids is not None else None)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        session = None
        try:
            session = login_session()
            live = (live_inventory(session, review_ids=selected)
                    if selected is not None else live_inventory(session))
        except Exception as exc:
            raise CommandError('Live inventory read failed: ' + type(exc).__name__) from exc
        finally:
            if session is not None:
                session.close()
        snapshot_started_at = datetime.now(timezone.utc).isoformat()
        snapshot = (read_snapshot(aptem_review_ids=selected)
                    if selected is not None else read_snapshot())
        snapshot_finished_at = datetime.now(timezone.utc).isoformat()
        report = audit_inventory(live, *snapshot,
                                 minimum_capture_version=options.get('minimum_capture_version', 6))
        report.update(snapshot_started_at=snapshot_started_at,
                      snapshot_finished_at=snapshot_finished_at,
                      audit_scope='selected_reviews' if selected is not None else 'all_reviews',
                      selected_review_ids=selected)
        report['scope'].update(
            completeness_claim='Selected reviews only; not a global completeness claim.'
                if selected is not None else 'All live reviews and the full stored snapshot.',
            canonical_learner_uniqueness='All stored learners, including in selected-review audits.',
            orphan_section_check='Selected existing parents only; global orphans without an Aptem parent ID are outside this audit.'
                if selected is not None else 'All stored physical review sections.')
        if options.get('report'):
            destination = Path(options['report']).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(json.dumps({key: report[key] for key in
                                      ('complete', 'totals', 'issue_counts', 'declared_answer_checks',
                                       'snapshot_started_at', 'snapshot_finished_at',
                                       'audit_scope', 'selected_review_ids')},
                                     sort_keys=True))
