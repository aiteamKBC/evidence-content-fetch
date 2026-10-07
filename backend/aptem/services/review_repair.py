"""Read live inventories and repair individual reviews without discarding successes."""
import re
from collections import Counter

from django.conf import settings
from django.db import connection

from aptem.management.commands.import_active_learner_reviews import metadata, planned_for, wait_review_list, advance_review_page
from .review_capture import capture_review
from .review_probe import check_session, ProbeError


def open_finished(page, learner_id, number=1):
    from playwright.sync_api import expect
    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/reviews/finished', wait_until='domcontentloaded')
    empty = wait_review_list(page)
    if empty:
        return False
    for current in range(2, number + 1):
        advance_review_page(page, current)
    return True


def finished_inventory(page, learner_id):
    from playwright.sync_api import expect
    if not open_finished(page, learner_id):
        return []
    inventory = []
    number = 1
    while True:
        rows = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True))
        if not rows.count():
            raise ProbeError('Nonempty Finished grid has no recognized rows')
        for index, row in enumerate(rows.all()):
            meta = metadata(page, row)
            if meta.get('Status') != 'Completed':
                raise ProbeError('Finished inventory includes an unexpected review status')
            inventory.append({'page': number, 'index': index, 'metadata': meta})
        pager_text = page.locator('kendo-pager').inner_text()
        total = re.search(r'of\s+(\d+)\s+items', pager_text)
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            if not total or len(inventory) != int(total[1]):
                raise ProbeError('Finished inventory count does not match pager total')
            break
        number += 1
        advance_review_page(page, number)
    return inventory


def read_finished_identity(page, learner_id, item):
    if not open_finished(page, learner_id, item['page']):
        raise ProbeError('Finished inventory disappeared')
    row = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True)).nth(item['index'])
    if metadata(page, row) != item['metadata']:
        raise ProbeError('Finished inventory changed during repair')
    row.get_by_role('button', name='View', exact=True).click()
    page.wait_for_url(re.compile(rf'/learners/{learner_id}/reviews/review-content/\d+$'))
    check_session(page)
    return page.url.rsplit('/', 1)[1]


def database_inventory(learner_id):
    try:
        with connection.cursor() as cursor:
            cursor.execute('''SELECT aptem_review_id,status,extraction_status,
                (SELECT count(*) FROM "Learner".review_sections s WHERE s.review_id=r.id),
                COALESCE((review_data->>'capture_version')::int,0)
                FROM "Learner".reviews r WHERE learner_id=%s''', [learner_id])
            rows = cursor.fetchall()
        ids = [str(r[0]) for r in rows]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate stored review identity')
        return {str(r[0]): {'status': r[1], 'extraction_status': r[2], 'sections': r[3], 'capture_version': r[4]} for r in rows}
    finally:
        connection.close()


def compare_inventory(expected, stored):
    missing = sorted(set(expected) - set(stored))
    extra = sorted(set(stored) - set(expected))
    invalid = []
    for key in set(expected) & set(stored):
        current = stored[key]
        status = expected[key]
        if current['status'] != status:
            invalid.append(key)
        elif status in ('Completed', 'In Progress') and (
            current['extraction_status'] != 'success' or current['sections'] == 0 or current['capture_version'] < 3
        ):
            invalid.append(key)
    return {'missing_ids': missing, 'extra_ids': extra, 'invalid_ids': sorted(invalid)}
