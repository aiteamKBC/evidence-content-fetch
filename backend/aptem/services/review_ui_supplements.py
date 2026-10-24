"""Refresh legacy rendered panels from Aptem's current, fully loaded review UI."""
import copy
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection

from .review_api_sync import (
    ReviewSyncError, _merge_preserved_sections, _set_manifest_verification,
)
from .review_probe import aptem_page, check_session

ACTION = 'VerifiedVisiblePanel'
SUPPORTED = {'reviews schedule', 'learning progress', 'personal learner record (plr)'}


def requested_panels(sections, previous):
    merged = _merge_preserved_sections(sections, previous)
    return sorted({s['section_name'] for s in merged
                   if (s.get('capture_state') == 'preserved_unverified' or s.get('source_action') == ACTION)
                   and s['section_name'].strip().lower() in SUPPORTED})


def _snapshot(panel):
    return panel.evaluate('''panel => {
        const text = e => (e.innerText || '').trim();
        return {
            raw_text: text(panel),
            fields: [...panel.querySelectorAll('input,textarea,select')]
                .filter(e => !['hidden','password'].includes(e.type))
                .map(e => ({label:[...(e.labels || [])].map(text).join(' ') ||
                    e.getAttribute('aria-label') || e.name || e.id || '',
                    type:e.type, value:e.value,
                    checked:['checkbox','radio'].includes(e.type) ? e.checked : null})),
            tables:[...panel.querySelectorAll('table')].map(t => ({
                rows:[...t.rows].map(r => [...r.cells].map(text))}))
        };
    }''')


def _settle(page, panel):
    from playwright.sync_api import expect
    page.wait_for_load_state('networkidle', timeout=60000)
    expect(panel.locator('[aria-busy="true"]')).to_have_count(0, timeout=45000)
    before = panel.inner_text()
    for _ in range(4):
        page.wait_for_timeout(400)
        after = panel.inner_text()
        if after == before:
            return
        before = after
    raise ReviewSyncError('Visible review panel has not finished rendering')


def _capture_panel(page, panel):
    """Capture every rendered page, checking its range and total, including zero."""
    _settle(page, panel)
    grids = panel.locator('kendo-grid')
    if grids.count() > 1:
        raise ReviewSyncError('Multiple visible grids require explicit pagination support')
    pages, expected_total, seen = [], None, 0
    if not grids.count():
        return [_snapshot(panel)]
    grid = grids.first
    for _ in range(1000):
        pager = grid.locator('kendo-pager-info')
        if pager.count() != 1:
            raise ReviewSyncError('Visible panel grid has no verifiable pager')
        match = re.search(r'(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+items', pager.inner_text())
        if not match:
            raise ReviewSyncError('Visible panel grid has an unrecognized pager')
        start, end, total = map(int, match.groups())
        if expected_total is None:
            expected_total = total
        if total != expected_total or (total and start != seen + 1) or end < start or end > total:
            raise ReviewSyncError('Visible panel inventory changed during pagination')
        if not total and (start or end):
            raise ReviewSyncError('Invalid empty panel grid range')
        if total:
            rows = grid.locator('tbody > tr').filter(has=page.locator('td[role="gridcell"]'))
            if rows.count() != end - start + 1:
                raise ReviewSyncError('Visible panel row count differs from pager')
        pages.append(_snapshot(panel))
        seen = end
        next_button = grid.get_by_role('button', name='Go to the next page', exact=True)
        if seen == total:
            if next_button.count() and next_button.is_enabled():
                raise ReviewSyncError('Panel pager has unexpected extra pages')
            return pages
        if next_button.count() != 1 or not next_button.is_enabled():
            raise ReviewSyncError('Panel grid is incomplete')
        next_button.click()
        from playwright.sync_api import expect
        expect(pager).to_contain_text(re.compile(r'^\s*' + str(end+1) + r'\s*-'), timeout=30000)
        _settle(page, panel)
    raise ReviewSyncError('Excessive panel pagination')


def capture_panels(learner_id, review_id, labels):
    captured = []
    url = settings.APTEM_BASE_URL.rstrip('/') + f'/pwa/learners/{learner_id}/reviews/review-content/{review_id}'
    with aptem_page() as page:
        source_status = {}
        relevant_paths = {f'/api/MWS.Review/{kind}/{review_id}'
                          for kind in ('ReviewContent', 'PlanReviews', 'LearningProgress')}
        relevant_paths.update({f'/api/Users/{learner_id}/PLR',
                               f'/api/Users/{learner_id}/PLR/PriorAttainmentLevel'})
        def received(response):
            parsed = urlsplit(response.url)
            if parsed.netloc == urlsplit(url).netloc and parsed.path in relevant_paths:
                source_status[parsed.path] = response.status
        page.on('response', received)
        page.goto(url, wait_until='domcontentloaded')
        check_session(page)
        page.wait_for_load_state('networkidle', timeout=60000)
        if page.url != url:
            raise ReviewSyncError('Visible review page identity changed')
        from playwright.sync_api import expect
        expect(page.locator('accordion-group').first).to_be_visible(timeout=45000)
        for label in labels:
            groups = page.locator('accordion-group')
            matches = [i for i, group in enumerate(groups.all())
                       if group.locator('div[role="button"][aria-expanded]').inner_text().strip().casefold()
                       == label.strip().casefold()]
            if len(matches) != 1:
                raise ReviewSyncError('Retained panel is not uniquely visible: ' + label)
            group = groups.nth(matches[0])
            toggle = group.locator('div[role="button"][aria-expanded]')
            if toggle.get_attribute('aria-expanded') == 'false':
                toggle.click()
            panel = group.get_by_role('tabpanel')
            expect(panel).to_be_visible()
            pages = _capture_panel(page, panel)
            if any(status != 200 for status in source_status.values()):
                raise ReviewSyncError('A visible review panel source request failed')
            if page.get_by_text(re.compile(r'^An error occurr?ed while executing requested action')).count():
                raise ReviewSyncError('Aptem displayed an error while rendering the review')
            captured.append({'section_name': label, 'source_action': ACTION,
                'source_kind': 'supplemental', 'capture_state': 'verified',
                'source_scope': 'current_visible_review_panel', 'source_url': url,
                'source_payload': {'review_id': str(review_id), 'learner_id': str(learner_id),
                    'checked_at': datetime.now(timezone.utc).isoformat(), 'pages': pages,
                    'source_http_status': dict(source_status)},
                'source_status': 'Readonly',
                'raw_text': '\n\n'.join(p['raw_text'] for p in pages),
                'fields': pages[0]['fields'], 'tables': [t for p in pages for t in p['tables']]})
    return captured


def enrich_visible_panels(learner_id, review_id, manifest, sections):
    with connection.cursor() as cursor:
        cursor.execute('SELECT review_data FROM "Learner".reviews WHERE aptem_review_id=%s', [str(review_id)])
        row = cursor.fetchone()
    if not row:
        return manifest, sections
    import json
    data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    previous = (data or {}).get('sections', [])
    labels = requested_panels(sections, previous)
    if not labels:
        return manifest, sections
    rendered = capture_panels(learner_id, review_id, labels)
    manifest, sections = copy.deepcopy(manifest), copy.deepcopy(sections)
    for section in rendered:
        # Retain complete historical content in metadata, even when the current UI is empty.
        section['historical_captures'] = [old for old in previous
            if old['section_name'] == section['section_name'] and old.get('source_action') != ACTION]
        prior_ui = [old for old in previous if old.get('source_action') == ACTION
                    and old['section_name'] == section['section_name']]
        if prior_ui:
            section['historical_captures'] = prior_ui[-1].get('historical_captures', [])
        sections.append(section)
        manifest.setdefault('supplemental_sections', []).append({key: section[key]
            for key in ('section_name', 'source_action', 'source_status')})
    _set_manifest_verification(manifest, sections)
    return manifest, sections
