from copy import deepcopy
import gzip
import json
import threading
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.conf import settings
from django.core.management.base import CommandError

from aptem.management.commands.repair_review_content import (
    Command, comparable_sections, expected_partial_sections, read_stored, record_unverified,
    verify_readback, verify_metadata_readback, write_backup,
)
from aptem.services.review_api_sync import ReviewSyncError


class ReviewContentReadbackTests(SimpleTestCase):
    def setUp(self):
        self.review = {'Id': 42}
        self.manifest = {'id': 42, 'capture_verified': True, 'sections': [{}],
                         'supplemental_sections': []}
        self.sections = [{'section_name': 'Questions', 'fields': [{'label': 'Agree', 'value': 'No'}],
                          'tables': [], 'raw_text': 'Agree\nNo', 'source_payload': {'agree': False},
                          'capture_state': 'verified'}]
        self.stored = {'review': {'aptem_review_id': '42', 'learner_id': 9,
                                  'extraction_status': 'success',
                                  'review_data': {'api_meta': deepcopy(self.manifest),
                                                  'capture_verified': True,
                                                  'sections': deepcopy(self.sections)}},
                       'sections': comparable_sections(self.sections)}

    def test_every_rendered_answer_and_original_response_must_match(self):
        self.assertEqual(len(verify_readback(self.stored, self.review, 9,
                                            self.manifest, self.sections)), 64)
        self.stored['sections'][0]['fields'] = [{'label': 'Agree', 'value': 'Yes'}]
        with self.assertRaisesRegex(ReviewSyncError, 'fields/tables/text'):
            verify_readback(self.stored, self.review, 9, self.manifest, self.sections)

    def test_original_payload_loss_is_detected_even_if_rendered_text_matches(self):
        self.stored['review']['review_data']['sections'][0]['source_payload'] = {}
        with self.assertRaisesRegex(ReviewSyncError, 'original source'):
            verify_readback(self.stored, self.review, 9, self.manifest, self.sections)

    def test_inaccessible_sections_cannot_be_reported_as_success(self):
        self.manifest['capture_verified'] = False
        self.stored['review']['review_data']['api_meta'] = deepcopy(self.manifest)
        with self.assertRaisesRegex(ReviewSyncError, 'unverified capture'):
            verify_readback(self.stored, self.review, 9, self.manifest, self.sections)

    def test_failed_capture_must_retain_prior_answers_and_source(self):
        before = deepcopy(self.stored)
        self.manifest['capture_verified'] = False
        self.sections[0].update(fields=[], raw_text='', capture_state='unavailable',
                                capture_error='HTTP 500', source_payload=None)
        expected = expected_partial_sections(self.sections, before)
        self.stored['review']['extraction_status'] = 'partial'
        self.stored['review']['review_data'].update(api_meta=deepcopy(self.manifest), capture_verified=False,
                                                  sections=deepcopy(expected))
        self.stored['sections'] = comparable_sections(expected)
        verify_readback(self.stored, self.review, 9, self.manifest, self.sections, before)
        self.stored['sections'][0]['fields'] = []
        with self.assertRaisesRegex(ReviewSyncError, 'prior content lost'):
            verify_readback(self.stored, self.review, 9, self.manifest, self.sections, before)
    def test_failed_capture_cannot_lose_last_verified_raw_source(self):
        before = deepcopy(self.stored)
        self.manifest['capture_verified'] = False
        self.sections[0].update(fields=[], raw_text='', capture_state='unavailable',
                                capture_error='HTTP 500', source_payload=None)
        expected = expected_partial_sections(self.sections, before)
        self.stored['review']['extraction_status'] = 'partial'
        self.stored['review']['review_data'].update(api_meta=deepcopy(self.manifest), capture_verified=False,
                                                  sections=deepcopy(expected))
        self.stored['sections'] = comparable_sections(expected)
        self.stored['review']['review_data']['sections'][0].pop('previous_source_payload')
        with self.assertRaisesRegex(ReviewSyncError, 'prior content lost'):
            verify_readback(self.stored, self.review, 9, self.manifest, self.sections, before)

    def test_complete_base_manifest_with_unverified_extra_content_stays_partial(self):
        before = deepcopy(self.stored)
        before['sections'].append({'section_name': 'Legacy Auxiliary', 'fields': [],
                                   'tables': [], 'raw_text': 'Keep this content'})
        expected = expected_partial_sections(self.sections, before)
        self.assertEqual(expected[-1]['capture_state'], 'preserved_unverified')
        self.stored['review']['extraction_status'] = 'partial'
        self.stored['review']['review_data'].update(capture_verified=False, sections=deepcopy(expected))
        self.stored['sections'] = comparable_sections(expected)
        verify_readback(self.stored, self.review, 9, self.manifest, self.sections, before)

    def test_readback_parent_and_children_use_one_ordered_database_snapshot(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (json.dumps(self.stored['review']),
                                       json.dumps(self.stored['sections']))
        with patch('aptem.management.commands.repair_review_content.connection') as database:
            database.cursor.return_value.__enter__.return_value = cursor
            self.assertEqual(read_stored('42'), self.stored)
        cursor.execute.assert_called_once()
        self.assertIn('ORDER BY s.section_order,s.id', cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], ['42'])


class ReviewContentResumeTests(SimpleTestCase):
    def test_failed_inventory_login_does_not_destroy_previous_checkpoint(self):
        with TemporaryDirectory() as directory:
            command = Command()
            command.path = Path(directory) / 'report.json'
            previous = {'results': {'42': {'outcome': 'verified'}}}
            command.path.write_text(json.dumps(previous), encoding='utf-8')
            with patch('aptem.management.commands.repair_review_content.login_session',
                       side_effect=ReviewSyncError('Aptem authentication failed')):
                with self.assertRaises(ReviewSyncError):
                    command.run({'resume': True, 'workers': 5,
                                 'scope': 'content', 'apply': True})
            self.assertEqual(json.loads(command.path.read_text(encoding='utf-8')), previous)


class ReviewContentFailureAuditTests(SimpleTestCase):
    @patch('aptem.management.commands.repair_review_content.transaction.atomic')
    @patch('aptem.management.commands.repair_review_content.connection')
    def test_source_failure_is_retained_without_replacing_saved_content(self, database, atomic):
        failure = {'method': 'GET', 'path': '/api/MWS.Review/ReviewContent/42',
                   'status_code': 500, 'attempts': 3}
        record_unverified('42', 'Content verification failed', source_failure=failure)
        cursor = database.cursor.return_value.__enter__.return_value
        query, params = cursor.execute.call_args.args
        audit = json.loads(params[1])
        self.assertFalse(audit['verified'])
        self.assertEqual(audit['source_failure'], failure)
        self.assertEqual(params[2], '42')
        self.assertIn("review_data=coalesce(review_data,'{}'::jsonb) ||", query)
        self.assertNotIn('DELETE', query)
        self.assertNotIn('review_sections', query)

    @patch('aptem.management.commands.repair_review_content.transaction.atomic')
    @patch('aptem.management.commands.repair_review_content.connection')
    def test_non_http_failure_does_not_invent_http_evidence(self, database, atomic):
        record_unverified('42', 'Readback mismatch')
        cursor = database.cursor.return_value.__enter__.return_value
        audit = json.loads(cursor.execute.call_args.args[1][1])
        self.assertFalse(audit['verified'])
        self.assertNotIn('source_failure', audit)


class ReviewContentBackupTests(SimpleTestCase):
    def test_completed_backup_is_readable_and_has_no_temporary_file(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / '42.json.gz'
            snapshot = {'review': {'aptem_review_id': '42'}, 'sections': []}
            write_backup(destination, snapshot)
            with gzip.open(destination, 'rt', encoding='utf-8') as stream:
                self.assertEqual(json.load(stream), snapshot)
            self.assertFalse(destination.with_name(destination.name + '.tmp').exists())

    def test_failed_write_never_publishes_an_incomplete_backup(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / '42.json.gz'
            with patch('aptem.management.commands.repair_review_content.json.dump',
                       side_effect=OSError('Simulated disk write failure')):
                with self.assertRaises(OSError):
                    write_backup(destination, {'review': None})
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + '.tmp').exists())

    def test_failed_replacement_does_not_destroy_existing_valid_backup(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / '42.json.gz'
            write_backup(destination, {'original': True})
            with patch.object(Path, 'replace', side_effect=PermissionError('Simulated lock')):
                with self.assertRaises(PermissionError):
                    write_backup(destination, {'original': False})
            with gzip.open(destination, 'rt', encoding='utf-8') as stream:
                self.assertEqual(json.load(stream), {'original': True})


class ReviewMetadataReadbackTests(SimpleTestCase):
    def setUp(self):
        self.review = {'Id': 42, 'LearnerId': 88, 'Status': 'NotScheduled',
                       'Name': ' New name ', 'Type': ' Progress ', 'OwnerName': 'Coach',
                       'PlannedDate': '2026-10-03T10:30:00Z', 'CompletedDate': None,
                       'UpdatedDate': '2026-10-03T01:00:00Z'}
        self.before = {
            'review': {'id': 7, 'aptem_review_id': '42', 'learner_id': 9,
                       'review_data': {'capture_verified': True, 'capture_version': 6,
                                       'content_source_updated_at': 'older',
                                       'api_meta': {'proof': True}, 'content_audit': {'verified': False},
                                       'sections': [{'source_payload': {'answer': False}}]},
                       'raw_text': 'Preserve answer', 'extraction_status': 'success',
                       'last_error': 'Preserve audit', 'last_extracted_at': '2026-10-01T10:00:00Z',
                       'future_content_column': {'retained': True}},
            'sections': [{'section_name': 'Question', 'fields': [{'value': 'No'}],
                          'tables': [], 'raw_text': 'Preserve answer'}],
            'physical_sections': [{'id': 321, 'review_id': 7, 'section_order': 1,
                                   'fields_json': [{'value': 'No'}], 'created_at': 'original'}],
        }
        self.after = deepcopy(self.before)
        self.after['review'].update(
            review_name='New name', review_type='Progress', reviewer_name='Coach',
            status='Not Scheduled', planned_scheduled_date='2026-10-03T10:30:00+00:00',
            completed_date=None,
            source_url=f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/88/reviews/review-content/42')
        self.after['review']['review_data'].update(aptem_learner_id='88', aptem_review_id='42',
                                                  live_odata=deepcopy(self.review))

    def test_metadata_refresh_retains_all_prior_content_and_does_not_reverify_it(self):
        self.assertEqual(len(verify_metadata_readback(self.after, self.review, 9, self.before)), 64)
        self.assertTrue(self.after['review']['review_data']['capture_verified'])
        self.assertFalse(self.after['review']['review_data']['content_audit']['verified'])

    def test_any_prior_json_change_is_rejected(self):
        for key in self.before['review']['review_data']:
            with self.subTest(key=key):
                changed = deepcopy(self.after)
                changed['review']['review_data'][key] = 'changed'
                with self.assertRaisesRegex(ReviewSyncError, 'prior content differs'):
                    verify_metadata_readback(changed, self.review, 9, self.before)

    def test_parent_content_fields_and_unknown_future_fields_cannot_change(self):
        for key in ('raw_text', 'extraction_status', 'last_error', 'last_extracted_at',
                    'future_content_column', 'id'):
            with self.subTest(key=key):
                changed = deepcopy(self.after)
                changed['review'][key] = 'changed'
                with self.assertRaisesRegex(ReviewSyncError, 'prior parent content'):
                    verify_metadata_readback(changed, self.review, 9, self.before)

    def test_same_rendered_content_does_not_allow_recreated_physical_rows(self):
        for key in ('id', 'created_at', 'fields_json'):
            with self.subTest(key=key):
                changed = deepcopy(self.after)
                changed['physical_sections'][0][key] = 'changed'
                with self.assertRaisesRegex(ReviewSyncError, 'physical sections'):
                    verify_metadata_readback(changed, self.review, 9, self.before)
        changed = deepcopy(self.after)
        changed.pop('physical_sections')
        with self.assertRaisesRegex(ReviewSyncError, 'physical sections'):
            verify_metadata_readback(changed, self.review, 9, self.before)

    def test_identity_source_metadata_status_and_dates_are_strict(self):
        for key in ('aptem_review_id', 'learner_id', 'review_name', 'review_type',
                    'reviewer_name', 'status', 'source_url', 'planned_scheduled_date', 'completed_date'):
            with self.subTest(key=key):
                changed = deepcopy(self.after)
                changed['review'][key] = 'wrong'
                with self.assertRaises(ReviewSyncError):
                    verify_metadata_readback(changed, self.review, 9, self.before)
        changed = deepcopy(self.after)
        changed['review']['review_data']['live_odata']['UpdatedDate'] = 'stale'
        with self.assertRaisesRegex(ReviewSyncError, 'source metadata'):
            verify_metadata_readback(changed, self.review, 9, self.before)

    def test_new_metadata_row_must_not_claim_any_content_capture(self):
        new = deepcopy(self.after)
        new['sections'], new['physical_sections'] = [], []
        new['review'].update(raw_text=None, extraction_status='partial', last_extracted_at=None)
        new['review']['review_data'] = {key: self.after['review']['review_data'][key]
                                       for key in ('aptem_review_id', 'aptem_learner_id', 'live_odata')}
        verify_metadata_readback(new, self.review, 9)
        for key, value in (('capture_verified', True), ('api_meta', {}), ('capture_version', 6)):
            with self.subTest(key=key):
                changed = deepcopy(new)
                changed['review']['review_data'][key] = value
                with self.assertRaisesRegex(ReviewSyncError, 'source metadata'):
                    verify_metadata_readback(changed, self.review, 9)
        new['review']['last_extracted_at'] = 'newly extracted'
        with self.assertRaisesRegex(ReviewSyncError, 'claims captured content'):
            verify_metadata_readback(new, self.review, 9)

    def test_physical_readback_uses_the_same_database_snapshot_as_parent(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (json.dumps(self.after['review']),
                                       json.dumps(self.after['sections']),
                                       json.dumps(self.after['physical_sections']))
        with patch('aptem.management.commands.repair_review_content.connection') as database:
            database.cursor.return_value.__enter__.return_value = cursor
            self.assertEqual(read_stored('42', include_physical=True), self.after)
        cursor.execute.assert_called_once()
        self.assertIn('jsonb_agg(to_jsonb(s) ORDER BY s.section_order,s.id)', cursor.execute.call_args.args[0])

    @patch('aptem.management.commands.repair_review_content.verify_metadata_readback')
    @patch('aptem.management.commands.repair_review_content.save_live_review')
    @patch('aptem.management.commands.repair_review_content.write_backup')
    @patch('aptem.management.commands.repair_review_content.read_stored')
    @patch('aptem.management.commands.repair_review_content.transaction.atomic')
    @patch('aptem.management.commands.repair_review_content.connection')
    def test_sync_save_and_readback_share_a_transaction_and_failed_readback_rolls_back(
            self, database, atomic, read, backup, save, verify):
        command = Command()
        command.report = {'scope': 'sync'}
        command.backup_dir = Path('unused-backups')
        read.side_effect = [self.before, self.after]
        verify.side_effect = ReviewSyncError('Metadata readback failed')
        with self.assertRaises(ReviewSyncError):
            command.save_and_verify(self.review, 9, None, None, metadata_only=True)
        save.assert_called_once_with(self.review, 9, None, None)
        self.assertEqual(atomic.return_value.__exit__.call_args.args[0], ReviewSyncError)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.kwargs == {'include_physical': True} for call in read.call_args_list))
        backup.assert_called_once_with(command.backup_dir/'42.json.gz', self.before)
        self.assertIn('FOR UPDATE', database.cursor.return_value.__enter__.return_value.execute.call_args.args[0])

    @patch('aptem.management.commands.repair_review_content.save_live_review')
    @patch('aptem.management.commands.repair_review_content.write_backup')
    @patch('aptem.management.commands.repair_review_content.read_stored')
    @patch('aptem.management.commands.repair_review_content.transaction.atomic')
    @patch('aptem.management.commands.repair_review_content.connection')
    def test_successful_metadata_report_never_claims_a_fresh_content_verification(
            self, database, atomic, read, backup, save):
        command = Command()
        command.report = {'scope': 'sync'}
        command.backup_dir = Path('unused-backups')
        read.side_effect = [self.before, self.after]
        result = command.save_and_verify(self.review, 9, None, None, metadata_only=True)
        self.assertEqual(result['outcome'], 'metadata_verified')
        self.assertIs(result['content_verified'], False)
        self.assertFalse(result['rendered_content_changed'])
        self.assertNotIn('source_capture_sha256', result)
        self.assertEqual(result['stored_section_count'], len(self.before['sections']))
        self.assertEqual(atomic.return_value.__exit__.call_args.args, (None, None, None))


class ReviewContentSyncPlanningTests(SimpleTestCase):
    def run_plan(self, live, *, selected='42', scope='sync', learners=None, existing=None, apply=False):
        with TemporaryDirectory() as directory:
            command = Command()
            command.path = Path(directory)/'report.json'
            command.mutex = threading.RLock()
            command.stdout = StringIO()
            command.checkpoint = MagicMock(wraps=command.checkpoint)
            with patch('aptem.management.commands.repair_review_content.login_session') as login, \
                 patch('aptem.management.commands.repair_review_content.live_inventory', return_value=live) as inventory, \
                 patch('aptem.management.commands.repair_review_content.connection') as database, \
                 patch('aptem.management.commands.repair_review_content.save_live_review') as save:
                cursor = database.cursor.return_value.__enter__.return_value
                cursor.fetchall.side_effect = [learners if learners is not None else [(9, 88)], existing or []]
                def completed_shard(index, tasks):
                    for review, learner_id in tasks:
                        command.report['results'][str(review['Id'])] = {
                            'outcome': 'verified' if review['Status'] == 'Completed' else 'metadata_verified'}
                command.worker = MagicMock(side_effect=completed_shard)
                try:
                    command.run({'scope': scope, 'review_ids': selected, 'resume': False,
                                 'workers': 2, 'apply': apply})
                finally:
                    save.assert_not_called()
                    if scope == 'sync' and selected:
                        inventory.assert_called_once_with(login.return_value,
                                                          review_ids=set(selected.split(',')))
                    else:
                        inventory.assert_called_once_with(login.return_value)
                return command

    def test_sync_selects_requested_ids_across_statuses_without_changing_content_scope(self):
        live = [{'Id': rid, 'LearnerId': 88, 'Status': status} for rid, status in (
            (42, 'Completed'), (43, 'Scheduled'), (44, 'NotScheduled'), (45, 'Scheduled'))]
        command = self.run_plan(live, selected='42,43,44')
        self.assertEqual(command.report['selected_total'], 3)
        command.worker.assert_not_called()
        old = self.run_plan(live, selected='42,43,44', scope='content')
        self.assertEqual(old.report['selected_total'], 1)

    def test_missing_explicit_id_fails_before_any_worker_or_write(self):
        with self.assertRaisesRegex(CommandError, 'missing from live inventory'):
            self.run_plan([{'Id': 42, 'LearnerId': 88, 'Status': 'Scheduled'}],
                          selected='42,999', apply=True)

    def test_duplicate_live_ids_fail_preflight(self):
        review = {'Id': 42, 'LearnerId': 88, 'Status': 'Scheduled'}
        with self.assertRaisesRegex(CommandError, 'duplicate review IDs'):
            self.run_plan([review, deepcopy(review)], apply=True)

    def test_duplicate_or_missing_canonical_mapping_fails_preflight(self):
        live = [{'Id': 42, 'LearnerId': 88, 'Status': 'Scheduled'}]
        with self.assertRaisesRegex(CommandError, 'Multiple canonical'):
            self.run_plan(live, learners=[(9, 88), (10, 88)], apply=True)
        with self.assertRaisesRegex(CommandError, 'no exact canonical'):
            self.run_plan(live, learners=[(9, 77)], apply=True)

    def test_duplicate_stored_review_id_and_unknown_status_fail_preflight(self):
        live = [{'Id': 42, 'LearnerId': 88, 'Status': 'Scheduled'}]
        with self.assertRaisesRegex(CommandError, 'Multiple stored reviews'):
            self.run_plan(live, existing=[('42',), ('42',)], apply=True)
        live[0]['Status'] = 'Unknown'
        with self.assertRaisesRegex(CommandError, 'unsupported sync status'):
            self.run_plan(live, apply=True)

    def test_mixed_content_and_metadata_results_finish_successfully_with_separate_counts(self):
        live = [{'Id': 42, 'LearnerId': 88, 'Status': 'Completed'},
                {'Id': 43, 'LearnerId': 88, 'Status': 'Scheduled'}]
        command = self.run_plan(live, selected='42,43', apply=True)
        self.assertEqual(command.report['run_status'], 'finished')
        self.assertEqual(command.report['results']['42']['outcome'], 'verified')
        self.assertEqual(command.report['results']['43']['outcome'], 'metadata_verified')
        self.assertEqual(command.report['counts'], {'verified': 1, 'metadata_verified': 1})

    @patch('aptem.management.commands.repair_review_content.record_unverified')
    @patch('aptem.management.commands.repair_review_content.write_backup')
    @patch('aptem.management.commands.repair_review_content.read_stored')
    @patch('aptem.management.commands.repair_review_content.fetch_review_content')
    @patch('aptem.management.commands.repair_review_content.login_session')
    @patch('aptem.management.commands.repair_review_content.connection')
    def test_metadata_worker_never_fetches_content_or_overwrites_content_audit_on_failure(
            self, database, login, fetch, read, backup, record):
        with TemporaryDirectory() as directory:
            command = Command()
            command.backup_dir = Path(directory)
            command.mutex = threading.RLock()
            command.checkpoint = MagicMock()
            command.report = {'scope': 'sync', 'active': {}, 'results': {}}
            command.save_and_verify = MagicMock(side_effect=ReviewSyncError('Metadata readback failed'))
            review = {'Id': 42, 'LearnerId': 88, 'Status': 'Scheduled'}
            command.worker(0, [(review, 9)])
        self.assertEqual(command.report['results']['42']['outcome'], 'error')
        command.save_and_verify.assert_called_once_with(review, 9, None, None, metadata_only=True)
        login.assert_not_called()
        fetch.assert_not_called()
        record.assert_not_called()
