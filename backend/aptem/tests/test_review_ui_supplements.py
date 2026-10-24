from copy import deepcopy
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase

from aptem.services.classic_review_inventory import _validated_visible_total
from aptem.services.review_probe import ProbeError
from aptem.services.review_api_sync import _merge_preserved_sections, capture_is_verified
from aptem.services.review_ui_supplements import ACTION, requested_panels, enrich_visible_panels


class VisibleSupplementTests(SimpleTestCase):
    def test_current_grid_total_requires_successful_matching_response(self):
        response = MagicMock(url='https://example.test/read', status=200)
        response.json.return_value = {'Data': [], 'Total': 0, 'Errors': None}
        self.assertEqual(_validated_visible_total({'readUrl': '/read', 'total': 0}, [response]), 0)
        with self.assertRaises(ProbeError):
            _validated_visible_total({'readUrl': '/other', 'total': 0}, [response])
        response.json.return_value['Total'] = 2
        with self.assertRaises(ProbeError):
            _validated_visible_total({'readUrl': '/read', 'total': 0}, [response])

    def test_historical_content_is_retained_when_live_panel_is_empty(self):
        old = {'section_name': 'Reviews Schedule', 'fields': [], 'tables': [],
               'raw_text': 'Old schedule and dates', 'capture_state': 'preserved_unverified'}
        current = {'section_name': 'Plan Reviews', 'source_action': 'PlanReviewsContent',
                   'source_payload': [], 'fields': [], 'tables': [], 'raw_text': '', 'capture_state': 'verified'}
        rendered = {'section_name': 'Reviews Schedule', 'source_action': ACTION,
                    'source_status': 'Readonly', 'source_payload': {'pages': []},
                    'fields': [], 'tables': [], 'raw_text': 'There are no reviews.', 'capture_state': 'verified'}
        manifest = {'sections': [{}], 'supplemental_sections': [], 'capture_verified': True}
        with patch('aptem.services.review_ui_supplements.connection') as db, \
                patch('aptem.services.review_ui_supplements.capture_panels', return_value=[deepcopy(rendered)]):
            db.cursor.return_value.__enter__.return_value.fetchone.return_value = ({'sections': [old]},)
            meta, sections = enrich_visible_panels('100', '10', manifest, [current])
        self.assertEqual(sections[-1]['historical_captures'], [old])
        merged = _merge_preserved_sections(sections, [old])
        self.assertTrue(capture_is_verified(meta, merged))
        self.assertEqual(merged[-1]['raw_text'], 'There are no reviews.')
        self.assertEqual(merged[-1]['historical_captures'][0]['raw_text'], 'Old schedule and dates')
        self.assertEqual(manifest['supplemental_sections'], [])

    def test_unknown_extra_section_is_not_auto_accepted(self):
        old = {'section_name': 'Unknown Extra Answers', 'raw_text': 'Must keep', 'fields': [], 'tables': []}
        self.assertEqual(requested_panels([], [old]), [])
        self.assertEqual(_merge_preserved_sections([], [old])[0]['capture_state'], 'preserved_unverified')

    def test_existing_browser_panel_is_requested_again(self):
        old = {'section_name': 'Learning Progress', 'source_action': ACTION,
               'source_payload': {'old': True}, 'raw_text': 'Progress', 'fields': [], 'tables': [],
               'capture_state': 'verified'}
        self.assertEqual(requested_panels([], [old]), ['Learning Progress'])
