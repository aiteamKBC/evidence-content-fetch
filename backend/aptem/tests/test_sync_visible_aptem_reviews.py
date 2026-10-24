import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from aptem.management.commands.sync_visible_aptem_reviews import verify_saved_visible
from aptem.services.review_api_sync import _payload


COMMAND = 'aptem.management.commands.sync_visible_aptem_reviews'


class VisibleReviewRetryTests(SimpleTestCase):
    def test_classic_payload_records_provenance_without_claiming_odata(self):
        review = {'Id': '4394', 'LearnerId': '4275',
                  'UpdatedDate': '2025-09-29T10:42:05',
                  'classic_extended': {'id': 4394, 'userId': 4275}}
        manifest = {'capture_verified': True, 'sections': [],
                    'supplemental_sections': []}
        data = _payload(review, manifest, [], source_kind='classic')
        self.assertEqual(data['capture_source'], 'classic-ui+review-api')
        self.assertEqual(data['visible_inventory_source'], 'classic_ui')
        self.assertNotIn('live_odata', data)

    def test_started_partial_review_blocks_cleanup(self):
        with patch(f'{COMMAND}.connection') as database:
            cursor = database.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [('4964', 'Completed', 'partial', 'false')]
            with self.assertRaisesRegex(Exception, 'unverified content'):
                verify_saved_visible(1, ['4964'])

    def test_failed_learner_is_retried_after_main_pass(self):
        seen = []

        def inventory(learner_id):
            seen.append(learner_id)
            if learner_id == '2' and seen.count('2') == 1:
                raise TimeoutError('Aptem temporarily unavailable')
            return [], 0, 0

        with TemporaryDirectory() as directory, \
                patch(f'{COMMAND}.canonical_mapping', return_value=(
                    {'1': 101, '2': 102, '3': 103}, set())), \
                patch(f'{COMMAND}.visible_ids', side_effect=inventory):
            path = Path(directory) / 'report.json'
            call_command('sync_visible_aptem_reviews', report=str(path))
            report = json.loads(path.read_text(encoding='utf-8'))

        self.assertEqual(seen, ['1', '2', '3', '2'])
        self.assertEqual(report['retry_queue'], ['2'])
        self.assertEqual(report['failed_learner_ids'], [])
        self.assertEqual(report['status'], 'finished')
        self.assertEqual(report['learners']['2']['attempt_count'], 2)
        self.assertEqual(len(report['learners']['2']['failure_history']), 1)

    def test_resume_defers_saved_failures_until_remaining_main_pass_finishes(self):
        with TemporaryDirectory() as directory, \
                patch(f'{COMMAND}.canonical_mapping', return_value=(
                    {'1': 101, '2': 102, '3': 103}, set())), \
                patch(f'{COMMAND}.visible_ids', return_value=([], 0, 0)) as inventory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps({
                'apply': False, 'worker_count': 1, 'worker_index': 0,
                'target_learners': 3,
                'learners': {
                    '1': {'status': 'complete'},
                    '2': {'status': 'failed', 'error': 'TimeoutError: old failure',
                          'finished_at': '2026-10-18T14:00:00+00:00'},
                },
            }), encoding='utf-8')
            call_command('sync_visible_aptem_reviews', report=str(path), resume=True)
            report = json.loads(path.read_text(encoding='utf-8'))

        self.assertEqual([call.args[0] for call in inventory.call_args_list], ['3', '2'])
        self.assertEqual(report['retry_queue'], ['2'])
        self.assertEqual(report['learners']['2']['failure_history'][0]['error'],
                         'TimeoutError: old failure')

    def test_retry_preserves_deletions_from_failed_first_attempt(self):
        with TemporaryDirectory() as directory, \
                patch(f'{COMMAND}.canonical_mapping', return_value=({'1': 101}, set())), \
                patch(f'{COMMAND}.visible_ids', return_value=(['10'], 1, 0)), \
                patch(f'{COMMAND}.prune_hidden', side_effect=[
                    {'reviews_deleted': 3}, {'reviews_deleted': 0}]), \
                patch(f'{COMMAND}.verify_saved_visible'), \
                patch(f'{COMMAND}.sync_selected', side_effect=[
                    ConnectionError('temporary database outage'),
                    {'saved_metadata': 1, 'saved_content': 0}]):
            path = Path(directory) / 'report.json'
            call_command('sync_visible_aptem_reviews', report=str(path), apply=True)
            report = json.loads(path.read_text(encoding='utf-8'))

        self.assertEqual(report['learners']['1']['deleted']['reviews_deleted'], 3)
        self.assertEqual(report['learners']['1']['saved']['saved_metadata'], 1)
        self.assertEqual(report['learners']['1']['status'], 'complete')

    def test_failed_source_runs_only_failed_learners_in_new_report(self):
        with TemporaryDirectory() as directory, \
                patch(f'{COMMAND}.canonical_mapping', return_value=(
                    {'1': 101, '2': 102, '3': 103}, set())), \
                patch(f'{COMMAND}.visible_ids', return_value=([], 0, 0)) as inventory:
            source = Path(directory) / 'prior.json'
            source.write_text(json.dumps({
                'apply': True, 'worker_count': 1, 'worker_index': 0,
                'status': 'finished_with_failures',
                'failed_learner_ids': ['2', '3'],
                'learners': {
                    '1': {'status': 'complete'},
                    '2': {'status': 'failed'},
                    '3': {'status': 'failed'},
                },
            }), encoding='utf-8')
            path = Path(directory) / 'failed-round.json'
            call_command('sync_visible_aptem_reviews', report=str(path),
                         failed_from_report=str(source))
            report = json.loads(path.read_text(encoding='utf-8'))

        self.assertEqual([call.args[0] for call in inventory.call_args_list], ['2', '3'])
        self.assertEqual(report['target_learners'], 2)
        self.assertEqual(report['source_failed_ids'], ['2', '3'])
        self.assertNotIn('1', report['learners'])
