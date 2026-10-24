"""Inventory review links actually rendered in Aptem Classic's Reviews grid."""
import re
from urllib.parse import urlsplit

from django.conf import settings

from .review_probe import ProbeError, check_session


class LiveWithdrawn(ProbeError):
    """The learner is Withdrawn on the live Aptem profile."""


def _grid_state(grid):
    return grid.evaluate('''element => {
        const data = window.jQuery?.(element).data('kendoGrid')?.dataSource;
        if (!data) return null;
        return {total: data.total(), page: data.page(), pages: data.totalPages(),
                pageSize: data.pageSize(), readUrl: data.transport?.options?.read?.url};
    }''')


def _page_rows(grid):
    return grid.locator(':scope > table > tbody > tr.k-master-row')


def _next_page(page, grid, current):
    next_link = grid.locator(':scope > .k-pager-wrap a[aria-label="Go to the next page"]')
    if next_link.count() != 1 or 'k-state-disabled' in (next_link.get_attribute('class') or ''):
        raise ProbeError('Classic review pagination stopped before the UI total')
    next_link.click()
    page.wait_for_function('''({selector, expected}) => {
        const grid = window.jQuery?.(selector).data('kendoGrid');
        return grid?.dataSource.page() === expected;
    }''', arg={'selector': '#' + grid.get_attribute('id'), 'expected': current + 1})
    page.wait_for_load_state('networkidle', timeout=60000)


def _review_ids(page, grid, expected):
    ids = []
    state = _grid_state(grid)
    if not state or state['total'] != expected or not state['pageSize']:
        raise ProbeError('Classic programme review count differs from its displayed count')
    for page_number in range(1, state['pages'] + 1):
        current = _grid_state(grid)
        if not current or current['page'] != page_number:
            raise ProbeError('Classic review page did not load')
        rows = _page_rows(grid)
        if rows.count() != min(current['pageSize'], expected - len(ids)):
            raise ProbeError('Classic review rows do not match the displayed page total')
        for row in rows.all():
            hrefs = row.locator('a[href^="/MWS.Review/Review/Get/"]').evaluate_all(
                'elements => elements.map(element => element.getAttribute("href"))')
            numbers = [re.fullmatch(r'/MWS\.Review/Review/Get/([1-9][0-9]*)', href or '')
                       for href in hrefs]
            if not numbers or any(match is None for match in numbers):
                raise ProbeError('Classic displayed review has no exact View identity')
            values = {match.group(1) for match in numbers}
            if len(values) != 1:
                raise ProbeError('Classic displayed review has conflicting View identities')
            ids.append(values.pop())
        if page_number < state['pages']:
            _next_page(page, grid, page_number)
    if len(ids) != expected or len(ids) != len(set(ids)):
        raise ProbeError('Classic displayed review inventory is incomplete or duplicated')
    return ids


def classic_visible_ids(page, learner_id, *, include_withdrawn=False):
    responses = []
    def received(response):
        if response.request.method == 'POST':
            responses.append(response)
    page.on('response', received)
    try:
        return _classic_visible_ids(page, learner_id, include_withdrawn=include_withdrawn,
                                    responses=responses)
    finally:
        page.remove_listener('response', received)


def _validated_visible_total(state, responses):
    """Programme header counts can include reviews the child grid does not expose."""
    path = urlsplit(state.get('readUrl') or '').path
    candidates = [r for r in responses if path and urlsplit(r.url).path == path]
    if not candidates or candidates[-1].status != 200:
        raise ProbeError('Visible programme reviews lack a successful grid response')
    data = candidates[-1].json()
    if (not isinstance(data, dict) or not isinstance(data.get('Data'), list)
            or type(data.get('Total')) is not int or data['Total'] < 0
            or data.get('Errors') not in (None, [], {})
            or len(data['Data']) > data['Total'] or data['Total'] != state['total']):
        raise ProbeError('Visible programme review response is invalid or differs from the grid')
    return data['Total']


def _classic_visible_ids(page, learner_id, *, include_withdrawn, responses):
    """Check current status, then walk every visible Classic programme and review page."""
    base = settings.APTEM_BASE_URL.rstrip('/')
    page.goto(f'{base}/pwa/learners/{learner_id}/profile/programme/details',
              wait_until='domcontentloaded')
    check_session(page)
    page.locator('[auto-id="view-profile-in-classic-button"]').wait_for(timeout=60000)
    page.wait_for_load_state('networkidle', timeout=60000)
    if page.get_by_text('Withdrawn', exact=True).is_visible() and not include_withdrawn:
        raise LiveWithdrawn('Live Aptem status is Withdrawn; learner left untouched')

    path = f'/MWS.ClientAdmin/JobSeekerProfile/Index/{learner_id}'
    page.goto(base + path, wait_until='domcontentloaded')
    page.wait_for_load_state('networkidle', timeout=60000)
    if not page.url.endswith(path) or page.title() != 'Kent Business College':
        raise ProbeError('Aptem Classic learner profile is unavailable')
    if page.get_by_text('User profile', exact=True).count() != 1:
        raise ProbeError('Aptem Classic learner identity is not displayed')
    reviews = page.get_by_text('Reviews', exact=True)
    if reviews.count() != 1:
        raise ProbeError('Aptem Classic Reviews section is unavailable')
    with page.expect_response(
        lambda response: urlsplit(response.url).path ==
        '/MWS.Review/Review/ReadReviewsEpisodes', timeout=60000
    ) as inventory_response:
        reviews.click()
    response = inventory_response.value
    if response.status != 200:
        raise ProbeError('Aptem Classic programme inventory request failed')
    proof = response.json()
    if (not isinstance(proof, dict) or not isinstance(proof.get('Data'), list)
            or not isinstance(proof.get('Total'), int)
            or proof['Total'] < 0 or proof.get('Errors') not in (None, [], {})):
        raise ProbeError('Aptem Classic programme inventory response is invalid')
    page.locator('#ReviewEpisodes').wait_for(timeout=60000)
    page.wait_for_load_state('networkidle', timeout=60000)
    page.wait_for_function('''expected => {
        const root = document.querySelector('#ReviewEpisodes');
        const data = window.jQuery?.(root).data('kendoGrid')?.dataSource;
        if (!data) return false;
        const rows = root.querySelectorAll(':scope > table > tbody > tr.k-master-row');
        const pager = root.querySelector(':scope > .k-pager-wrap');
        return data.total() === expected &&
            (expected > 0 ? rows.length > 0 :
                rows.length === 0 && pager && /of\\s+0/.test(pager.innerText));
    }''', arg=proof['Total'], timeout=60000)
    outer = page.locator('#ReviewEpisodes')
    state = _grid_state(outer)
    if not state or state['total'] != proof['Total'] or not state['pageSize']:
        raise ProbeError('Aptem Classic Reviews grid did not finish loading')
    if state['total'] == 0:
        return [], 0, 0

    ids = []
    processed = 0
    for page_number in range(1, state['pages'] + 1):
        current = _grid_state(outer)
        if not current or current['page'] != page_number:
            raise ProbeError('Classic programme page did not load')
        rows = _page_rows(outer)
        if rows.count() != min(current['pageSize'], state['total'] - processed):
            raise ProbeError('Classic programme rows do not match the displayed page total')
        for index in range(rows.count()):
            row = _page_rows(outer).nth(index)
            label = row.locator(':scope > td[role="gridcell"]').inner_text().strip()
            match = re.search(r'\(\s*(\d+)\s*\)\s*$', label)
            if not match:
                raise ProbeError('Classic programme has no displayed review count')
            expected = int(match.group(1))
            response_start = len(responses)
            if row.locator(':scope > td.k-hierarchy-cell a.k-i-expand').count():
                row.locator(':scope > td.k-hierarchy-cell a.k-i-expand').click()
            detail = row.locator('xpath=following-sibling::tr[1]')
            grid = detail.locator('div.ReviewsInEpisode')
            grid.wait_for(timeout=60000)
            page.wait_for_load_state('networkidle', timeout=60000)
            page.wait_for_function('''selector => {
                const root = document.querySelector(selector);
                const data = window.jQuery?.(root).data('kendoGrid')?.dataSource;
                const rows = root?.querySelectorAll(':scope > table > tbody > tr.k-master-row');
                return data && !data._requestInProgress &&
                    (data.total() === 0 || rows?.length > 0);
            }''', arg='#' + grid.get_attribute('id'), timeout=60000)
            expected = _validated_visible_total(_grid_state(grid), responses[response_start:])
            ids.extend(_review_ids(page, grid, expected))
            processed += 1
        if page_number < state['pages']:
            _next_page(page, outer, page_number)
    if processed != state['total']:
        raise ProbeError('Classic learner inventory is incomplete')
    # Transfers can expose the same review under two programme episodes.
    # Each individual grid is still strictly checked for duplicate/missing rows.
    ids = list(dict.fromkeys(ids))
    return ids, 0, len(ids)
