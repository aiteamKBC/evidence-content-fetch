import json
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from aptem.services.review_probe import aptem_page, check_session
from aptem.services.review_storage import destination_learner, save_review


class Command(BaseCommand):
    help = 'Import all Betty Planned list metadata without starting or editing reviews.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        from playwright.sync_api import expect
        try:
            destination_learner(15997)
            payloads = []
            seen = set()
            with aptem_page() as page:
                base = settings.APTEM_BASE_URL.rstrip('/')
                page.goto(base+'/pwa/learners/15997/dashboard', wait_until='domcontentloaded')
                check_session(page)
                expect(page.get_by_text('Active', exact=True)).to_be_visible()
                check_session(page)
                page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
                page.wait_for_url('**/learners/15997/reviews/upcoming')
                pager = page.locator('kendo-pager')
                expect(pager).to_be_visible()
                number = 1
                while True:
                    expect(page.locator('[aria-busy="true"]')).to_have_count(0)
                    rows = page.get_by_role('row').filter(has=page.locator('a[href*="/tasks/assistant/reviews/schedule/"]'))
                    expect(rows.first).to_be_visible()
                    for row in rows.all():
                        meta = {}
                        for header in page.get_by_role('columnheader').all():
                            col = header.get_attribute('aria-colindex')
                            if col and col.isdigit():
                                cell = row.locator(f'[role="gridcell"][aria-colindex="{col}"]')
                                if cell.count() == 1:
                                    meta[' '.join(header.inner_text().split())] = cell.inner_text().strip()
                        href = row.locator('a[href*="/tasks/assistant/reviews/schedule/"]').get_attribute('href')
                        match = re.fullmatch(r'/pwa/learners/15997/tasks/assistant/reviews/schedule/(\d+)', href or '')
                        if not match or match[1] in seen:
                            raise ValueError('Missing or duplicate Planned review identifier')
                        seen.add(match[1])
                        names = [x.strip() for x in meta['Review Name / Review Type'].splitlines() if x.strip()]
                        payloads.append({'aptem_learner_id':'15997','aptem_review_id':match[1],
                            'programme_status':'Active','review_category':'planned',
                            'status':meta['Status'],'review_name':names[0],'review_type':names[-1],
                            'reviewer_name':meta.get('Reviewer'),'source_url':page.url,
                            'source_metadata':meta,'identity_href':href,'sections':[],
                            'raw_text':row.inner_text(),'extraction_scope':'list_metadata_only'})
                    next_button = page.get_by_role('button', name='Go to the next page', exact=True)
                    if not next_button.is_enabled():
                        break
                    previous = rows.all_text_contents()
                    next_button.click()
                    number += 1
                    expect(pager).to_have_attribute('aria-label', re.compile(rf'Page navigation, page {number} of \d+'))
                    expect(rows.first).not_to_have_text(previous[0])
                page.screenshot(path=str(settings.BASE_DIR.parent/'debug'/'planned-last-page.png'), full_page=True)
            path = settings.BASE_DIR.parent/'debug'/'betty-planned-reviews.json'
            path.write_text(json.dumps(payloads, indent=2), encoding='utf-8')
            self.stdout.write(f'[PLANNED] found={len(payloads)} pages={number}')
            if not options['dry_run']:
                for payload in payloads:
                    self.stdout.write(json.dumps({'aptem_review_id':payload['aptem_review_id'], **save_review(payload)}))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
