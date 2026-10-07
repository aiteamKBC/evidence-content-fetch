"""Import Planned metadata and all Finished reviews for live Active Aptem learners."""
import json
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from aptem.services.learner_reviews import capture_all_finished
from aptem.services.review_capture import capture_review
from aptem.services.review_probe import aptem_page, check_session, ProbeError
from aptem.services.review_storage import destination_learner, save_review


def active_ids(limit=None, learner_id=None):
    sql = '''SELECT "ID" FROM "LMS"."Aptem_users"
             WHERE "Program-Status" = 'Active' AND "ID" IS NOT NULL
             ORDER BY "ID"'''
    args = []
    if learner_id:
        sql = sql.replace('ORDER BY', 'AND "ID" = %s ORDER BY')
        args.append(str(learner_id))
    if limit:
        sql += ' LIMIT %s'
        args.append(int(limit))
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return [str(row[0]) for row in cursor.fetchall()]


def metadata(page, row):
    # Read the row and its headers in one DOM operation, never across a page swap.
    return row.evaluate('''row => {
        const result = {}, grid = row.closest('kendo-grid');
        if (!grid) throw new Error('Review row is outside its grid');
        for (const header of grid.querySelectorAll('[role="columnheader"]')) {
            const col = header.getAttribute('aria-colindex');
            if (!col || !/^\\d+$/.test(col)) continue;
            const cells = row.querySelectorAll('[role="gridcell"][aria-colindex="'+col+'"]');
            if (cells.length === 1) result[header.innerText.trim().replace(/\\s+/g,' ')] = cells[0].innerText.trim();
        }
        return result;
    }''')


def advance_review_page(page, page_number):
    from playwright.sync_api import expect
    signature = "JSON.stringify([...document.querySelectorAll('kendo-grid [role=gridcell]')].map(e=>e.innerText))"
    before = page.evaluate('() => ' + signature)
    page.get_by_role('button', name='Go to the next page', exact=True).click()
    expect(page.locator('kendo-pager')).to_have_attribute(
        'aria-label', re.compile(rf'Page navigation, page {page_number} of \d+'))
    page.wait_for_function('previous => ' + signature + ' !== previous', arg=before)
    wait_review_list(page)


def wait_review_list(page):
    """An explicit empty grid is evidence of zero; missing rows alone are not."""
    from playwright.sync_api import expect
    check_session(page)
    # Angular renders an empty grid before its data request has finished. Waiting
    # only for headers or aria-busy can mistake that transient state for no reviews.
    page.wait_for_load_state('networkidle', timeout=60000)
    expect(page.get_by_role('columnheader').first).to_be_visible()
    expect(page.locator('[aria-busy="true"]')).to_have_count(0)
    page.wait_for_function('''() => {
        const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
        return [...document.querySelectorAll('[role="gridcell"], .k-grid-norecords')]
            .some(e => visible(e) && e.innerText.trim());
    }''')
    empty = page.locator('.k-grid-norecords')
    if empty.count() == 1 and empty.is_visible() and bool(empty.inner_text().strip()):
        page.wait_for_timeout(1000)
        page.wait_for_load_state('networkidle', timeout=60000)
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
        return empty.count() == 1 and empty.is_visible() and bool(empty.inner_text().strip())
    return False


def planned_for(page, learner_id, on_review=None):
    from playwright.sync_api import expect
    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
    check_session(page)
    expect(page.get_by_text('Active', exact=True)).to_be_visible()
    page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
    page.wait_for_url(f'**/learners/{learner_id}/reviews/upcoming')
    if wait_review_list(page):
        return []
    rows = page.get_by_role('row').filter(has=page.locator('a[href*="/tasks/assistant/reviews/schedule/"]'))
    payloads = []
    seen = set()
    page_number = 1
    while True:
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
        page.wait_for_timeout(1000)
        for row in rows.all():
            link_locator = row.locator('a[href*="/tasks/assistant/reviews/schedule/"]')
            if link_locator.count() != 1:
                raise ProbeError('Planned review identity is ambiguous')
            meta = metadata(page, row)
            href = link_locator.get_attribute('href') or ''
            match = re.search(r'/schedule/(\d+)$', href)
            if not match:
                raise ProbeError('Planned review ID is missing')
            if match[1] in seen:
                raise ProbeError('Duplicate Planned review ID or pagination did not advance')
            seen.add(match[1])
            names = [x.strip() for x in meta.get('Review Name / Review Type', '').splitlines() if x.strip()]
            payloads.append({'aptem_learner_id': learner_id, 'aptem_review_id': match[1],
                'programme_status': 'Active', 'review_category': 'planned',
                'status': meta.get('Status'), 'review_name': names[0] if names else '',
                'review_type': names[-1] if names else '', 'reviewer_name': meta.get('Reviewer'),
                'source_url': page.url, 'source_metadata': meta, 'identity_href': href,
                'sections': [], 'raw_text': row.inner_text(), 'extraction_scope': 'list_metadata_only'})
            if on_review:
                on_review(payloads[-1])
        if rows.count() == 0:
            raise ProbeError('Planned grid contains no recognized review rows')
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            total = re.search(r'of\s+(\d+)\s+items', page.locator('kendo-pager').inner_text())
            if not total or len(payloads) != int(total[1]):
                raise ProbeError('Planned inventory count does not match pager total')
            break
        page_number += 1
        advance_review_page(page, page_number)
    return payloads


def finished_for(page, learner_id, on_review=None, on_inventory=None):
    from playwright.sync_api import expect
    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
    check_session(page)
    expect(page.get_by_text('Active', exact=True)).to_be_visible()
    page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
    page.wait_for_url(f'**/learners/{learner_id}/reviews/upcoming')
    page.get_by_role('button', name='Finished', exact=True).click()
    page.wait_for_url(f'**/learners/{learner_id}/reviews/finished')
    if wait_review_list(page):
        if on_inventory:
            on_inventory(0)
        return []
    row = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True)).first
    expect(row).to_be_visible()
    first_meta = metadata(page, row)
    row.get_by_role('button', name='View', exact=True).click()
    page.wait_for_url(re.compile(rf'/learners/{learner_id}/reviews/review-content/\d+$'))
    review_id = page.url.rsplit('/', 1)[1]
    payloads = capture_all_finished(page, learner_id, review_id, first_meta, write=print,
                                    on_review=on_review, on_inventory=on_inventory)
    return payloads


class Command(BaseCommand):
    help = 'Import all Planned and Finished reviews for database Active Aptem learners.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--learner-id')
        parser.add_argument('--limit', type=int)
        parser.add_argument('--all-active', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if not options['all_active'] and not options['learner_id'] and not options['limit']:
            raise CommandError('Choose --learner-id, --limit, or --all-active; default batch is intentionally disabled')
        ids = active_ids(options['limit'], options['learner_id'])
        stats = {'active_learners_found': len(ids), 'learners_verified_active': 0,
                 'learners_skipped_not_active': 0, 'learners_failed': 0,
                 'finished_reviews_found': 0, 'reviews_extracted': 0, 'reviews_failed': 0}
        for learner_id in ids:
            try:
                payloads = []
                with aptem_page() as page:
                    from playwright.sync_api import expect
                    page.goto(f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
                    check_session(page)
                    page.wait_for_load_state('networkidle', timeout=30000)
                    status = page.get_by_text('Active', exact=True)
                    if status.count() == 0 or not any(status.nth(i).is_visible() for i in range(status.count())):
                        stats['learners_skipped_not_active'] += 1
                        self.stdout.write(f'[SKIP] learner {learner_id} status=not Active')
                        continue
                    stats['learners_verified_active'] += 1
                    self.stdout.write(f'[ACTIVE] learner {learner_id}')
                    planned = planned_for(page, learner_id)
                    finished = finished_for(page, learner_id)
                    payloads = planned + finished
                    stats['finished_reviews_found'] += len(finished)
                    stats['reviews_extracted'] += len(finished)
                    self.stdout.write(f'[REVIEWS] learner {learner_id} planned={len(planned)} finished={len(finished)}')
                if not options['dry_run']:
                    for payload in payloads:
                        save_review(payload)
            except Exception as exc:
                stats['learners_failed'] += 1
                self.stdout.write(f'[FAILED] learner {learner_id} reason={exc}')
        self.stdout.write(json.dumps(stats))
