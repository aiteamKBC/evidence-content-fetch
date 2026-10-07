"""Read all Finished reviews for the already verified learner, including pagination."""
import re

from django.conf import settings

from .review_capture import capture_review
from .review_probe import ProbeError, check_session


def capture_all_finished(page, learner_id, first_id, first_metadata, write=print,
                         on_review=None, on_inventory=None):
    from playwright.sync_api import expect

    payloads = [capture_review(page, learner_id, first_id, first_metadata)]
    if on_review:
        on_review(payloads[0])
    seen = {first_id}
    base = settings.APTEM_BASE_URL.rstrip('/')
    finished_url = f'{base}/pwa/learners/{learner_id}/reviews/finished'

    def open_list(page_number):
        page.goto(f'{base}/pwa/learners/{learner_id}/dashboard', wait_until='domcontentloaded')
        expect(page.get_by_text('Active', exact=True)).to_be_visible()
        check_session(page)
        page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
        page.get_by_role('button', name='Finished', exact=True).click()
        page.wait_for_url(finished_url)
        expect(page.get_by_role('button', name='View', exact=True).first).to_be_visible()
        for number in range(1, page_number):
            page.get_by_role('button', name='Go to the next page', exact=True).click()
            expect(page.locator('kendo-pager')).to_have_attribute(
                'aria-label', re.compile(rf'Page navigation, page {number+1} of \d+'))
            expect(page.locator('[aria-busy="true"]')).to_have_count(0)

    open_list(1)
    inventory = []
    page_number = 1
    while True:
        rows = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True))
        for index, row in enumerate(rows.all()):
            expect(row.get_by_role('gridcell', name='Completed', exact=True)).to_be_visible()
            metadata = {}
            for header in page.get_by_role('columnheader').all():
                col = header.get_attribute('aria-colindex')
                if col and col.isdigit():
                    cell = row.locator(f'[role="gridcell"][aria-colindex="{col}"]')
                    if cell.count() == 1:
                        metadata[' '.join(header.inner_text().split())] = cell.inner_text().strip()
            inventory.append((page_number, index, metadata))
        next_button = page.get_by_role('button', name='Go to the next page', exact=True)
        if not next_button.is_enabled():
            break
        page_number += 1
        next_button.click()
        expect(page.locator('kendo-pager')).to_have_attribute(
            'aria-label', re.compile(rf'Page navigation, page {page_number} of \d+'))
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
    write(f'[REVIEWS] learner {learner_id} finished={len(inventory)}')
    if on_inventory:
        on_inventory(len(inventory))
    for number, index, metadata in inventory:
        if number == 1 and index == 0:
            if metadata != first_metadata:
                raise ProbeError('Finished review list changed during extraction')
            continue
        open_list(number)
        row = page.get_by_role('row').filter(has=page.get_by_role('button', name='View', exact=True)).nth(index)
        # Position is used only to enumerate rows; verify metadata before opening.
        current = {}
        for header in page.get_by_role('columnheader').all():
            col = header.get_attribute('aria-colindex')
            if col and col.isdigit():
                cell = row.locator(f'[role="gridcell"][aria-colindex="{col}"]')
                if cell.count() == 1:
                    current[' '.join(header.inner_text().split())] = cell.inner_text().strip()
        if current != metadata:
            raise ProbeError('Finished review list changed during extraction')
        row.get_by_role('button', name='View', exact=True).click()
        page.wait_for_url(re.compile(rf'/learners/{learner_id}/reviews/review-content/\d+$'))
        review_id = page.url.rsplit('/', 1)[1]
        if review_id in seen:
            raise ProbeError('Duplicate review ID in Finished inventory')
        toggles = page.locator('div[role="button"][aria-expanded]')
        expect(toggles.first).to_be_visible()
        payloads.append(capture_review(page, learner_id, review_id, metadata))
        if on_review:
            on_review(payloads[-1])
        seen.add(review_id)
        page.screenshot(path=str(settings.BASE_DIR.parent/'debug'/f'review-{review_id}.png'), full_page=True)
        write(f'[EXTRACTED] review {review_id}')
    if len(payloads) != len(inventory):
        raise ProbeError('Extracted review count does not match Finished inventory')
    return payloads
