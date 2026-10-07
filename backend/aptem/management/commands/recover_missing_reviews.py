"""Recover the 12 Completed reviews that save_review's validator refused.

For each of a known list of learners: opens their Aptem Finished list, discovers
which review IDs are Completed there, and compares against what is already in
Learner.reviews with status='Completed' and extraction_status='success'.

For any Completed aptem_review_id whose DB row is either absent or in a weaker
state (extraction_status != 'success', or Completed but raw_text shorter than a
few hundred characters), navigate directly to the review page, extract the full
sections, and write it. Rows that are already Completed + success + substantial
content are never touched.

Runs --dry-run by default. Pass --apply to actually write.
"""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from aptem.services.review_capture import capture_review
from aptem.services.review_probe import aptem_page, check_session


# aptem_id -> display name (source of truth is the audit checkpoint; this is
# just for a friendly log line)
TARGET_LEARNERS = {
    '4344': 'Ted Claxton',
    '4449': 'Thomas Emblem',
    '4521': 'Laura Baxter',
    '4737': 'Gemma Phillips',
    '4841': 'Toby Allen',
    '5053': 'Leigh Millington',
    '6105': 'Eleanor Cross',
    '6737': 'Jonwilliam Macintyre',
    '5170': 'Abbie Nicol',
    '4035': 'Elliot Phillips',
    '4947': 'Evangeline Rice',
    '6511': 'Georgia Fenn',
}

# A Completed + success row is only considered "already covered" above this
# raw_text length. Below it, we treat the DB row as a stub worth replacing.
COVERED_MIN_RAW_LEN = 1000


def destination_learner_id(cursor, aptem_id):
    cursor.execute(
        '''SELECT l.id FROM "Learner".learners l
           JOIN "LMS"."Aptem_users" a
             ON lower(trim(l.email)) = lower(trim(a."Email"))
           WHERE a."ID" = %s AND a."Program-Status" = 'Active' ''',
        [aptem_id],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    return rows[0][0]


def db_review_snapshot(cursor, learner_id):
    cursor.execute(
        '''SELECT id, aptem_review_id, status, extraction_status,
                  length(coalesce(raw_text, '')) AS raw_len
           FROM "Learner".reviews WHERE learner_id = %s''',
        [learner_id],
    )
    rows = cursor.fetchall()
    return {row[1]: {'pk': row[0], 'status': row[2], 'extraction': row[3], 'raw_len': row[4]} for row in rows}


def read_finished_inventory(page, learner_id):
    """Read every review that appears in the Finished list, across all pages,
    returning a list of dicts with aptem_review_id and its row metadata."""
    from playwright.sync_api import expect

    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
    check_session(page)
    expect(page.get_by_text('Active', exact=True)).to_be_visible()
    page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
    page.get_by_role('button', name='Finished', exact=True).click()
    page.wait_for_url(f'**/learners/{learner_id}/reviews/finished')
    expect(page.get_by_role('button', name='View', exact=True).first).to_be_visible()

    inventory = []
    seen_ids = set()
    page_number = 1
    while True:
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
        page.wait_for_timeout(500)
        rows = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True))
        for row in rows.all():
            metadata = {}
            for header in page.get_by_role('columnheader').all():
                col = header.get_attribute('aria-colindex')
                if col and col.isdigit():
                    cell = row.locator(f'[role="gridcell"][aria-colindex="{col}"]')
                    if cell.count() == 1:
                        metadata[' '.join(header.inner_text().split())] = cell.inner_text().strip()
            # The direct review link isn't easy to read from the row (buttons open on click).
            # Instead we click View, capture the URL, then go back.
            inventory.append({'row_index': len(inventory), 'metadata': metadata})
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            break
        page_number += 1
        next_button.click()
        expect(page.locator('kendo-pager')).to_have_attribute(
            'aria-label', re.compile(rf'Page navigation, page {page_number} of \d+'))
    return inventory


def open_review_by_index(page, learner_id, index):
    """Open the Finished list and click the View button for the given row index,
    returning the aptem_review_id from the resulting URL."""
    from playwright.sync_api import expect

    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/reviews/finished', wait_until='domcontentloaded')
    check_session(page)
    expect(page.get_by_role('button', name='View', exact=True).first).to_be_visible()
    # advance pages until we can reach the wanted index
    rows_per_page = None
    target_page = 1
    row_in_page = index
    while True:
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
        current_rows = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True)).count()
        if rows_per_page is None:
            rows_per_page = current_rows
        if row_in_page < current_rows:
            break
        row_in_page -= current_rows
        target_page += 1
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            raise ValueError('Row index beyond Finished pagination')
        next_button.click()
        expect(page.locator('kendo-pager')).to_have_attribute(
            'aria-label', re.compile(rf'Page navigation, page {target_page} of \d+'))
    row = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True)).nth(row_in_page)
    row.get_by_role('button', name='View', exact=True).click()
    page.wait_for_url(re.compile(rf'/learners/{learner_id}/reviews/review-content/\d+$'))
    review_id = page.url.rsplit('/', 1)[1]
    return review_id


def force_write(cursor, learner_id, payload, existing_pk):
    """Write the captured review directly. If existing_pk is None, INSERT;
    otherwise UPDATE the existing row and replace its sections. This bypasses
    validate_replacement, so it must only be called when the caller has
    verified the existing row is a low-content stub."""
    sections = payload['sections']
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    values = [
        payload['review_name'], payload['review_type'], payload['reviewer_name'],
        payload['status'], payload['source_url'], encoded, payload['raw_text'], digest,
    ]
    if existing_pk:
        cursor.execute(
            '''UPDATE "Learner".reviews SET review_name=%s, review_type=%s,
                 reviewer_name=%s, status=%s, source_url=%s, review_data=%s::jsonb,
                 raw_text=%s, content_hash=%s, extraction_status='success',
                 last_error=NULL, last_seen_at=now(), last_extracted_at=now(),
                 updated_at=now()
               WHERE id=%s''',
            values + [existing_pk],
        )
        review_pk = existing_pk
        cursor.execute('DELETE FROM "Learner".review_sections WHERE review_id=%s', [review_pk])
    else:
        cursor.execute(
            '''INSERT INTO "Learner".reviews
                 (review_name, review_type, reviewer_name, status, source_url,
                  review_data, raw_text, content_hash, learner_id, aptem_review_id,
                  last_extracted_at)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, now())
               RETURNING id''',
            values + [learner_id, payload['aptem_review_id']],
        )
        review_pk = cursor.fetchone()[0]
    cursor.execute(
        '''UPDATE "Learner".reviews r
             SET learner_name = l.full_name, learner_email = l.email
             FROM "Learner".learners l
             WHERE r.id = %s AND l.id = r.learner_id''',
        [review_pk],
    )
    for order, section in enumerate(sections, 1):
        section_fields = list(section['fields'])
        if section.get('links'):
            section_fields.append({'label': 'Attachments', 'value': '', 'links': section['links']})
        cursor.execute(
            '''INSERT INTO "Learner".review_sections
                 (review_id, section_name, section_order, fields_json, tables_json, raw_text)
               VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)''',
            [
                review_pk, section['section_name'], order,
                json.dumps(section_fields), json.dumps(section['tables']),
                section['raw_text'],
            ],
        )
    return review_pk


class Command(BaseCommand):
    help = 'Recover the 12 Completed reviews save_review previously refused.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Actually write to the database. Without this flag, runs dry-run.')
        parser.add_argument('--learner-ids', default='',
                            help='Comma-separated aptem IDs to process. Defaults to all 12.')

    def handle(self, *args, **options):
        apply_mode = bool(options['apply'])
        prefix = '[APPLY]' if apply_mode else '[DRY-RUN]'
        filter_ids = {s.strip() for s in options['learner_ids'].split(',') if s.strip()}
        targets = {k: v for k, v in TARGET_LEARNERS.items() if not filter_ids or k in filter_ids}
        self.stdout.write(f'{prefix} Recovering missing reviews for {len(targets)} learners: {list(targets)}')
        TARGET_LEARNERS_LOCAL = targets

        report = {'per_learner': [], 'summary': {
            'inserted': 0, 'replaced_stub': 0, 'skipped_already_covered': 0,
            'skipped_no_upgrade': 0, 'errors': 0,
        }}

        for aptem_id, display_name in TARGET_LEARNERS_LOCAL.items():
            entry = {'aptem_id': aptem_id, 'name': display_name, 'actions': [], 'errors': []}
            try:
                with connection.cursor() as cursor:
                    learner_id = destination_learner_id(cursor, aptem_id)
                    if not learner_id:
                        entry['errors'].append('no destination learner')
                        report['summary']['errors'] += 1
                        report['per_learner'].append(entry)
                        continue
                    db_by_review = db_review_snapshot(cursor, learner_id)

                with aptem_page() as page:
                    inventory = read_finished_inventory(page, aptem_id)
                    self.stdout.write(f'{prefix} {display_name} (aptem={aptem_id}) finished_count={len(inventory)}')
                    # First pass: click through each row, learn its aptem_review_id + status,
                    # decide action, then only fetch the ones that need recovery.
                    known_finished = []
                    for idx, item in enumerate(inventory):
                        review_id = open_review_by_index(page, aptem_id, idx)
                        known_finished.append((idx, review_id, item['metadata']))
                    # Decide per review
                    for idx, review_id, meta in known_finished:
                        db_row = db_by_review.get(review_id)
                        status = meta.get('Status', '')
                        if status != 'Completed':
                            continue
                        if db_row and db_row['status'] == 'Completed' \
                                and db_row['extraction'] == 'success' \
                                and db_row['raw_len'] >= COVERED_MIN_RAW_LEN:
                            entry['actions'].append({
                                'review_id': review_id, 'decision': 'skip_already_covered',
                                'db_raw_len': db_row['raw_len'],
                            })
                            report['summary']['skipped_already_covered'] += 1
                            continue
                        # Need to fetch full review and either insert or replace
                        review_id_confirmed = open_review_by_index(page, aptem_id, idx)
                        if review_id_confirmed != review_id:
                            entry['errors'].append(
                                f'row {idx} yielded different review id ({review_id_confirmed} vs {review_id})')
                            report['summary']['errors'] += 1
                            continue
                        payload = capture_review(page, aptem_id, review_id, meta)
                        new_raw_len = len(payload['raw_text'])
                        if db_row and db_row['status'] == 'Completed' \
                                and db_row['extraction'] == 'success' \
                                and db_row['raw_len'] >= new_raw_len:
                            entry['actions'].append({
                                'review_id': review_id, 'decision': 'skip_no_upgrade',
                                'db_raw_len': db_row['raw_len'], 'new_raw_len': new_raw_len,
                            })
                            report['summary']['skipped_no_upgrade'] += 1
                            continue
                        action = {
                            'review_id': review_id,
                            'new_raw_len': new_raw_len,
                            'new_sections': len(payload['sections']),
                            'db_before': db_row,
                        }
                        if apply_mode:
                            # Playwright owns an asyncio loop on this thread; run the DB
                            # write in a helper thread so Django's ORM/connection is happy.
                            def _write(payload=payload, learner_id=learner_id, db_row=db_row):
                                try:
                                    with transaction.atomic(), connection.cursor() as cursor:
                                        cursor.execute('LOCK TABLE "Learner".reviews IN SHARE ROW EXCLUSIVE MODE')
                                        return force_write(cursor, learner_id, payload,
                                                           db_row['pk'] if db_row else None)
                                finally:
                                    connection.close()
                            with ThreadPoolExecutor(max_workers=1) as executor:
                                pk = executor.submit(_write).result()
                            action['written_pk'] = pk
                            if db_row:
                                action['decision'] = 'replaced_stub'
                                report['summary']['replaced_stub'] += 1
                            else:
                                action['decision'] = 'inserted'
                                report['summary']['inserted'] += 1
                        else:
                            action['decision'] = 'would_' + ('replace_stub' if db_row else 'insert')
                            if db_row:
                                report['summary']['replaced_stub'] += 1
                            else:
                                report['summary']['inserted'] += 1
                        entry['actions'].append(action)
            except Exception as exc:
                entry['errors'].append(f'{type(exc).__name__}: {exc}')
                report['summary']['errors'] += 1
            report['per_learner'].append(entry)
            self.stdout.write(f'{prefix} {display_name}: '
                              f'actions={len(entry["actions"])} errors={len(entry["errors"])}')

        print(json.dumps(report['summary'], indent=2))
        suffix = '-' + '-'.join(sorted(targets)) if len(targets) < len(TARGET_LEARNERS) else ''
        out_path = settings.BASE_DIR.parent / 'debug' / f'recover_missing_report{suffix}.json'
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(f'{prefix} Full report: {out_path}')
