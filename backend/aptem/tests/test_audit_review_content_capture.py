import copy
import json
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from aptem.management.commands.audit_review_content_capture import (
    Command, SECTION_SNAPSHOT_SQL, audit_inventory, declared_answer_audit, read_snapshot, same_timestamp,
)


class ReviewContentAuditTests(SimpleTestCase):
    def setUp(self):
        self.source = {'Id': 1, 'LearnerId': 10, 'Status': 'Completed',
                       'UpdatedDate': '2026-10-01T12:00:00Z'}
        self.payload = {'fields': [{'name': 'answer', 'title': 'Answer'}], 'answer': False}
        self.fields = [{'label': 'Answer', 'value': 'No'}]
        self.row = {'id': 100, 'aptem_review_id': '1', 'learner_id': 50,
                    'status': 'Completed', 'extraction_status': 'success', 'json_section_count': 1,
                    'data': {'capture_version': 6, 'content_capture_version': 6,
                             'capture_verified': True, 'aptem_review_id': '1', 'aptem_learner_id': '10',
                             'content_source_updated_at': self.source['UpdatedDate'],
                             'live_odata': self.source.copy(), 'api_meta': {
                                 'id': 1, 'userId': 10, 'capture_verified': True,
                                 'supplemental_sections': [],
                                 'sections': [{'name': 'Custom', 'customName': 'Questions',
                                               'contentRoute': {'action': 'CustomContent'}}]}}}
        self.section = {'order': 1, 'name': 'Questions', 'capture_state': 'verified',
                        'source_action': 'CustomContent', 'has_source_payload': True,
                        'name_matches': True, 'fields_match': True, 'tables_match': True,
                        'text_match': True, 'custom_payload': self.payload, 'custom_fields': self.fields}

    def audit(self):
        return audit_inventory([self.source], {50: 10}, [self.row], {100: [self.section]})

    def test_correct_capture_passes_independent_checks(self):
        result = self.audit()
        self.assertTrue(result['complete'])
        self.assertEqual(result['declared_answer_checks']['false_answers'], 1)
        self.assertEqual(result['totals']['verified_current_content_reviews'], 1)

    def use_valid_plr_section(self):
        self.row['data']['api_meta']['sections'][0]['contentRoute']['action'] = 'PLRContent'
        self.section.update(source_action='PLRContent', custom_payload=None, custom_fields=None,
                            plr_capture={
            'source_contract': 'plr-ui-v1', 'source_scope': 'current_learner_plr',
            'source_learner_id': '10',
            'source_endpoints': {
                'learner_identity': '/odata/1.0/Users',
                'qualifications': '/api/Users/10/PLR',
                'prior_attainment': '/api/Users/10/PLR/PriorAttainmentLevel'},
            'source_payload': {
                'learner_identity': {'value': [{'Id': 10, 'UserILRSummary_ULN': None}]},
                'qualifications': [],
                'prior_attainment': {'userId': 10, 'englishLevel': 0, 'mathsLevel': None,
                                     'ictLevel': None, 'selectedHighestLevel': None,
                                     'learnerSelectedHighestLevel': None,
                                     'isIlrUserFieldsReadonly': False}}})

    def test_plr_current_learner_contract_accepts_empty_records_and_null_uln(self):
        self.use_valid_plr_section()
        self.assertTrue(self.audit()['complete'])

    def test_old_plr_prior_learning_response_cannot_pass_mirror_checks(self):
        self.use_valid_plr_section()
        self.section['plr_capture'] = {
            'source_endpoint': '/api/MWS.Review/PriorLearning/1', 'source_payload': []}
        result = self.audit()
        self.assertFalse(result['complete'])
        self.assertEqual(result['issues']['plr_source_contract_invalid'], ['1'])
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_plr_requires_each_raw_source_and_exact_learner_identity(self):
        self.use_valid_plr_section()
        original = copy.deepcopy(self.section)
        invalid_captures = []
        for key in ('source_contract', 'source_scope', 'source_learner_id',
                    'source_endpoints', 'source_payload'):
            capture = copy.deepcopy(original['plr_capture'])
            capture.pop(key)
            invalid_captures.append(capture)
        for key in ('learner_identity', 'qualifications', 'prior_attainment'):
            capture = copy.deepcopy(original['plr_capture'])
            capture['source_payload'].pop(key)
            invalid_captures.append(capture)
        for bad_value in ({'value': []}, {'value': [{'Id': 11, 'UserILRSummary_ULN': None}]},
                          {'value': [{'Id': 10}]}):
            capture = copy.deepcopy(original['plr_capture'])
            capture['source_payload']['learner_identity'] = bad_value
            invalid_captures.append(capture)
        for bad_value in ({}, [{'userId': 11}], [None]):
            capture = copy.deepcopy(original['plr_capture'])
            capture['source_payload']['qualifications'] = bad_value
            invalid_captures.append(capture)
        for bad_value in ({'userId': 10}, {'userId': 11}):
            capture = copy.deepcopy(original['plr_capture'])
            capture['source_payload']['prior_attainment'] = bad_value
            invalid_captures.append(capture)
        capture = copy.deepcopy(original['plr_capture'])
        capture['source_payload']['prior_attainment_error'] = None
        invalid_captures.append(capture)
        capture = copy.deepcopy(original['plr_capture'])
        capture['source_endpoints']['qualifications'] = '/api/MWS.Review/PriorLearning/1'
        invalid_captures.append(capture)
        for index, capture in enumerate(invalid_captures):
            with self.subTest(case=index):
                self.section['plr_capture'] = capture
                self.assertEqual(self.audit()['issues']['plr_source_contract_invalid'], ['1'])
        self.section = copy.deepcopy(original)
        self.section['capture_state'] = 'unavailable'
        self.assertIn('plr_source_contract_invalid', self.audit()['issues'])
        self.section = copy.deepcopy(original)
        self.section['has_source_payload'] = False
        self.assertIn('plr_source_contract_invalid', self.audit()['issues'])

    def test_missing_plr_row_still_returns_targeted_repair_id(self):
        self.use_valid_plr_section()
        result = audit_inventory([self.source], {50: 10}, [self.row], {})
        self.assertEqual(result['issues']['plr_source_contract_invalid'], ['1'])

    def test_wrong_plr_source_disqualifies_history_only_bucket(self):
        self.use_valid_plr_section()
        self.section['plr_capture'] = None
        sections = self.historical_only_snapshot()
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_plr_source_projection_flows_through_snapshot_mapping(self):
        self.use_valid_plr_section()
        target = 'aptem.management.commands.audit_review_content_capture.'
        parent = (100, '1', 50, 'Completed', 'success', json.dumps(self.row['data']), 1)
        section = (100, 1, 'Questions', 'verified', 'PLRContent', True,
                   True, True, True, True, None, None, None, None,
                   json.dumps(self.section['plr_capture']))
        with patch(target + 'connection') as mocked_connection, patch(target + 'transaction.atomic'):
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[(50, 10)], [(100,)], [parent], [(100,)], [section], []]
            snapshot = read_snapshot()
        self.assertEqual(snapshot[2][100][0]['plr_capture'], self.section['plr_capture'])
        self.assertTrue(audit_inventory([self.source], *snapshot)['complete'])

    def test_false_zero_and_duplicate_label_values_are_preserved(self):
        payload = {'fields': [{'name': 'a', 'title': 'Same'}, {'name': 'b', 'title': 'Same'}],
                   'a': False, 'b': 0}
        issues, counts = declared_answer_audit(payload, [{'label': 'Same', 'value': 'No'},
                                                        {'label': 'Same [2]', 'value': '0'}])
        self.assertEqual(issues, [])
        self.assertEqual(counts['false_answers'], 1)
        self.assertEqual(counts['zero_answers'], 1)
        self.assertEqual(counts['duplicate_answer_labels'], 1)

    def test_options_are_not_an_answer_and_false_is_not_empty(self):
        payload = {'fields': [{'name': 'a', 'title': 'A', 'listAnswers': ['Yes', 'No']}], 'a': None}
        issues, counts = declared_answer_audit(payload, [{'label': 'A', 'value': '["Yes", "No"]'}])
        self.assertEqual(issues, ['extra_rendered_answers'])
        self.assertEqual(counts['empty_values_with_options'], 1)

    def test_repeated_equal_answers_must_keep_multiplicity(self):
        payload = {'fields': [{'name': 'a', 'title': 'X'}, {'name': 'b', 'title': 'X'}],
                   'a': 'same', 'b': 'same'}
        issues, counts = declared_answer_audit(payload, [{'label': 'X', 'value': 'same'}])
        self.assertIn('missing_direct_answers', issues)
        self.assertEqual(counts['missing_direct_answers'], 1)

    def test_swapped_values_under_different_labels_are_detected(self):
        payload = {'fields': [{'name': 'a', 'title': 'A'}, {'name': 'b', 'title': 'B'}],
                   'a': 'first', 'b': 'second'}
        issues, counts = declared_answer_audit(payload, [{'label': 'A', 'value': 'second'},
                                                        {'label': 'B', 'value': 'first'}])
        self.assertEqual(counts['missing_direct_answers'], 2)
        self.assertEqual(counts['extra_rendered_answers'], 2)
        self.assertIn('missing_direct_answers', issues)

    def test_static_display_mapping_collision_keeps_values_associated(self):
        payload = {'fields': [{'name': 'a', 'title': 'fullName'}, {'name': 'b', 'title': 'Name'}],
                   'a': 'first', 'b': 'second'}
        issues, _ = declared_answer_audit(payload, [{'label': 'Name', 'value': 'first'},
                                                  {'label': 'Name [2]', 'value': 'second'}])
        self.assertEqual(issues, [])

    def test_literal_suffix_title_collision_is_resolved_independently(self):
        payload = {'fields': [{'name': 'a', 'title': 'A'}, {'name': 'b', 'title': 'A'},
                              {'name': 'c', 'title': 'A [2]'}], 'a': '1', 'b': '2', 'c': '3'}
        issues, _ = declared_answer_audit(payload, [{'label': 'A', 'value': '1'},
                                                  {'label': 'A [2]', 'value': '2'},
                                                  {'label': 'A [2] [2]', 'value': '3'}])
        self.assertEqual(issues, [])

    def test_identity_status_and_freshness_fail_independently(self):
        self.row['learner_id'] = 51
        self.row['status'] = 'Scheduled'
        self.row['data']['content_source_updated_at'] = '2025-01-01T00:00:00Z'
        self.row['data']['api_meta']['userId'] = 11
        result = self.audit()
        for key in ('canonical_learner_mismatch', 'status_mismatch',
                    'content_source_updated_date_mismatch', 'manifest_identity_mismatch'):
            self.assertEqual(result['issues'][key], ['1'])

    def test_unavailable_section_is_not_complete(self):
        self.section['capture_state'] = 'unavailable_preserved'
        self.row['data']['capture_verified'] = False
        self.row['extraction_status'] = 'partial'
        result = self.audit()
        self.assertFalse(result['complete'])
        self.assertIn('section_source_unverified', result['issues'])

    def test_reconstructed_manifest_is_explicitly_unverified(self):
        self.row['data']['api_meta']['manifest_reconstructed'] = True
        self.assertIn('manifest_unverified', self.audit()['issues'])

    def test_missing_meeting_requires_exact_source_evidence(self):
        self.section.update(capture_state='verified_source_absent',
                            source_action='MeetingDetailsContent',
                            absence_evidence={'extended': {'id': 1, 'userId': 10},
                                              'meeting_response': {'message':
                                                  'There is no corresponding Review meeting'}})
        self.row['data']['api_meta']['sections'][0]['contentRoute']['action'] = 'MeetingDetailsContent'
        self.assertTrue(self.audit()['complete'])
        self.section['absence_evidence']['meeting_response']['message'] = 'Temporary failure'
        self.assertIn('source_absence_evidence_invalid', self.audit()['issues'])

    def test_db_and_json_differences_are_reported(self):
        self.section['tables_match'] = False
        self.row['json_section_count'] = 2
        issues = self.audit()['issues']
        self.assertIn('section_tables_json_table_mismatch', issues)
        self.assertIn('section_cardinality_mismatch', issues)

    def test_v5_cannot_prove_supplemental_ui_sections_complete(self):
        self.row['data']['content_capture_version'] = 5
        self.row['data']['api_meta'].pop('supplemental_sections')
        issues = self.audit()['issues']
        self.assertIn('old_content_capture_version', issues)
        self.assertIn('supplemental_source_inventory_unverified', issues)

    def test_preserved_extra_section_is_unverified_not_lost(self):
        preserved = dict(self.section, order=2, source_kind='historical_preserved',
                         capture_state='preserved_unverified')
        self.row['json_section_count'] = 2
        result = audit_inventory([self.source], {50: 10}, [self.row],
                                 {100: [self.section, preserved]})
        self.assertIn('preserved_extra_sections_unverified', result['issues'])
        self.assertNotIn('section_cardinality_mismatch', result['issues'])

    def historical_only_snapshot(self):
        self.row['data']['capture_verified'] = False
        self.row['extraction_status'] = 'partial'
        self.row['json_section_count'] = 2
        prior = dict(self.section, order=2, source_kind='historical_preserved',
                     capture_state='preserved_unverified', has_source_payload=False,
                     custom_payload=None, custom_fields=None)
        return {100: [self.section, prior]}

    def test_verified_source_with_intact_unverified_history_has_separate_bucket(self):
        sections = self.historical_only_snapshot()
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['verified_current_content_reviews'], 0)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 1)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 0)
        self.assertFalse(result['complete'])
        self.assertIn('preserved_extra_sections_unverified', result['issues'])
        self.assertIn('section_source_unverified', result['issues'])

    def test_unavailable_auxiliary_disqualifies_history_only_bucket(self):
        sections = self.historical_only_snapshot()
        self.row['data']['api_meta']['supplemental_sections'] = [
            {'section_name': 'Summary', 'source_action': 'ReviewSummarySupplemental'}]
        missing_summary = dict(self.section, order=2, name='Summary',
                               source_action='ReviewSummarySupplemental', source_kind='supplemental',
                               capture_state='unavailable')
        sections[100][1]['order'] = 3
        sections[100].insert(1, missing_summary)
        self.row['json_section_count'] = 3
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_stale_capture_disqualifies_history_only_bucket(self):
        sections = self.historical_only_snapshot()
        self.row['data']['content_source_updated_at'] = '2020-01-01T00:00:00Z'
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_corrupt_history_storage_is_not_classified_as_intact_retention(self):
        sections = self.historical_only_snapshot()
        sections[100][1]['fields_match'] = False
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_unknown_extra_kind_is_not_the_known_retained_history_bucket(self):
        sections = self.historical_only_snapshot()
        sections[100][1]['source_kind'] = None
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_separate_failed_source_audit_disqualifies_history_only_bucket(self):
        sections = self.historical_only_snapshot()
        self.row['data']['content_audit'] = {'verified': False, 'reason': 'Content verification failed'}
        result = audit_inventory([self.source], {50: 10}, [self.row], sections)
        self.assertEqual(result['totals']['current_source_verified_with_unverified_history_reviews'], 0)
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 1)

    def test_supplemental_sections_must_be_present_and_named(self):
        self.row['data']['api_meta']['supplemental_sections'] = [
            {'section_name': 'Summary', 'source_action': 'MeetingSummary'}]
        self.row['json_section_count'] = 2
        supplemental = dict(self.section, order=2, name='Summary',
                            source_action='MeetingSummary', source_kind='supplemental')
        result = audit_inventory([self.source], {50: 10}, [self.row],
                                 {100: [self.section, supplemental]})
        self.assertTrue(result['complete'])

    def test_missing_duplicate_historical_and_orphan_are_classified(self):
        historical = copy.deepcopy(self.row)
        historical.update(id=101, aptem_review_id='99', status='Archived')
        result = audit_inventory([self.source, {'Id': 2, 'LearnerId': 10, 'Status': 'Archived'}],
                                 {50: 10}, [self.row, self.row, historical], {}, [909])
        self.assertEqual(result['issues']['missing_review'], ['2'])
        self.assertEqual(result['issues']['duplicate_aptem_review_id'], ['1'])
        self.assertEqual(result['historical_extras'], [{'review_id': '99', 'archived': True}])
        self.assertEqual(result['orphan_section_review_ids'], [909])
        self.assertEqual(result['totals']['unverified_current_source_reviews'], 2)
        self.assertEqual(sum(result['totals'][key] for key in (
            'verified_current_content_reviews',
            'current_source_verified_with_unverified_history_reviews',
            'unverified_current_source_reviews')), result['totals']['content_scope_reviews'])

    def test_timestamp_offsets_compare_as_instants(self):
        self.assertTrue(same_timestamp('2026-10-01T12:00:00Z', '2026-10-01T15:00:00+03:00'))
        self.assertFalse(same_timestamp('broken', 'broken'))

    def test_metadata_only_scope_does_not_claim_sections_empty(self):
        self.source['Status'] = 'NotScheduled'
        self.row['status'] = 'Not Scheduled'
        self.row['data']['live_odata']['Status'] = 'NotScheduled'
        result = self.audit()
        self.assertEqual(result['totals']['metadata_scope_reviews'], 1)
        self.assertTrue(result['scope']['metadata_only_statuses_do_not_prove_empty_content'])

    def test_command_closes_session_and_uses_read_only_snapshot(self):
        session = MagicMock()
        command = Command(stdout=StringIO())
        target = 'aptem.management.commands.audit_review_content_capture.'
        with patch(target + 'login_session', return_value=session), \
                patch(target + 'live_inventory', return_value=[self.source]), \
                patch(target + 'read_snapshot', return_value=({50: 10}, [self.row],
                                                              {100: [self.section]}, [])):
            command.handle(report=None)
        session.close.assert_called_once()
        self.assertIn('"complete": true', command.stdout.getvalue())

    def test_snapshot_is_explicitly_read_only_and_repeatable(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        with patch(target + 'connection') as mocked_connection, \
                patch(target + 'transaction.atomic'):
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[], [], [], []]
            read_snapshot()
        first_statement = cursor.execute.call_args_list[0].args[0]
        self.assertEqual(first_statement,
                         'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertTrue(all(call.args[0].lstrip().startswith('SELECT')
                            for call in cursor.execute.call_args_list[1:]))

    def test_command_reports_snapshot_bounds_without_changing_snapshot_api(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        session = MagicMock()
        command = Command(stdout=StringIO())
        started = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 10, 2, 0, 1, 0, tzinfo=timezone.utc)
        with patch(target + 'login_session', return_value=session), \
                patch(target + 'live_inventory', return_value=[self.source]), \
                patch(target + 'read_snapshot', return_value=({50: 10}, [self.row],
                                                              {100: [self.section]}, [])) as snapshot, \
                patch(target + 'datetime') as clock:
            clock.now.side_effect = [started, finished, finished]
            command.handle(report=None)
        report = json.loads(command.stdout.getvalue())
        self.assertEqual(report['snapshot_started_at'], started.isoformat())
        self.assertEqual(report['snapshot_finished_at'], finished.isoformat())
        snapshot.assert_called_once_with()

    def test_large_section_projection_is_chunked_in_one_snapshot(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        section_row = (101, 1, 'Q', 'verified', 'CustomContent', True, True, True,
                       True, True, '{"fields":[]}', '[]', None, None)
        with patch(target + 'connection') as mocked_connection, \
                patch(target + 'transaction.atomic') as atomic:
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[], [], [(101,), (102,), (103,)],
                                            [section_row], [], []]
            _, _, sections, _ = read_snapshot(section_batch_size=2)
        batches = [call for call in cursor.execute.call_args_list if call.args[0] == SECTION_SNAPSHOT_SQL]
        self.assertEqual([call.args[1] for call in batches],
                         [[[101, 102], [101, 102]], [[103], [103]]])
        atomic.assert_called_once()
        self.assertEqual(sections[101][0]['custom_payload'], {'fields': []})
        self.assertTrue(sections[101][0]['fields_match'])
        self.assertIn('AS MATERIALIZED', SECTION_SNAPSHOT_SQL)
        self.assertEqual(SECTION_SNAPSHOT_SQL.count("review_data->'sections'"), 1)

    def test_section_chunk_size_is_bounded(self):
        for value in (0, 201):
            with self.assertRaises(ValueError):
                read_snapshot(section_batch_size=value)

    def test_parent_json_projection_is_also_bounded(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        parent = (1, '1', 50, 'Completed', 'success', '{}', 0)
        with patch(target + 'connection') as mocked_connection, patch(target + 'transaction.atomic'):
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[], [(number,) for number in range(1, 202)],
                                            [parent], [], [], []]
            _, reviews, _, _ = read_snapshot()
        batches = [call for call in cursor.execute.call_args_list
                   if 'jsonb_build_object(' in call.args[0]]
        self.assertEqual([len(call.args[1][0]) for call in batches], [200, 1])
        self.assertEqual(reviews[0]['data'], {})

    def test_snapshot_sql_mapping_flows_through_full_audit(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        parent = (100, '1', 50, 'Completed', 'success', json.dumps(self.row['data']), 1)
        section = (100, 1, 'Questions', 'verified', 'CustomContent', True,
                   True, True, True, True, json.dumps(self.payload),
                   json.dumps(self.fields), None, None)
        with patch(target + 'connection') as mocked_connection, patch(target + 'transaction.atomic'):
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[(50, 10)], [(100,)], [parent], [(100,)], [section], []]
            snapshot = read_snapshot()
        entry = snapshot[2][100][0]
        for key in ('name_matches', 'fields_match', 'tables_match', 'text_match'):
            self.assertIs(entry[key], True)
        result = audit_inventory([self.source], *snapshot)
        self.assertTrue(result['complete'], result['issues'])
        self.assertEqual(result['totals']['verified_current_content_reviews'], 1)

    def test_sql_reported_text_mismatch_is_not_hidden_by_mapping_fix(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        parent = (100, '1', 50, 'Completed', 'success', json.dumps(self.row['data']), 1)
        section = (100, 1, 'Questions', 'verified', 'CustomContent', True,
                   True, True, True, False, json.dumps(self.payload),
                   json.dumps(self.fields), None, None)
        with patch(target + 'connection') as mocked_connection, patch(target + 'transaction.atomic'):
            cursor = mocked_connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[(50, 10)], [(100,)], [parent], [(100,)], [section], []]
            result = audit_inventory([self.source], *read_snapshot())
        self.assertEqual(result['issues']['section_text_json_table_mismatch'], ['1'])

    def test_selected_command_scopes_both_reads_and_reports_missing_database_target(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        source_two = dict(self.source, Id=2)
        session = MagicMock()
        command = Command(stdout=StringIO())
        with patch(target + 'login_session', return_value=session), \
                patch(target + 'live_inventory', return_value=[self.source, source_two]) as inventory, \
                patch(target + 'read_snapshot', return_value=({50: 10}, [self.row],
                                                              {100: [self.section]}, [])) as snapshot, \
                patch(target + 'Path') as report_path:
            command.handle(report='unused.json', review_ids='2, 1,2')
        inventory.assert_called_once_with(session, review_ids=['1', '2'])
        snapshot.assert_called_once_with(aptem_review_ids=['1', '2'])
        session.close.assert_called_once()
        report = json.loads(report_path.return_value.resolve.return_value.write_text.call_args.args[0])
        self.assertFalse(report['complete'])
        self.assertEqual(report['issues']['missing_review'], ['2'])
        self.assertEqual(report['audit_scope'], 'selected_reviews')
        self.assertEqual(report['selected_review_ids'], ['1', '2'])
        self.assertIn('not a global completeness claim', report['scope']['completeness_claim'])
        output = json.loads(command.stdout.getvalue())
        self.assertEqual(output['selected_review_ids'], ['1', '2'])
        self.assertEqual(output['issue_counts']['missing_review'], 1)

    def test_selected_success_is_explicitly_not_a_whole_database_audit(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        command = Command(stdout=StringIO())
        with patch(target + 'login_session'), \
                patch(target + 'live_inventory', return_value=[self.source]), \
                patch(target + 'read_snapshot', return_value=({50: 10}, [self.row],
                                                              {100: [self.section]}, [])):
            command.handle(report=None, review_ids='1')
        output = json.loads(command.stdout.getvalue())
        self.assertTrue(output['complete'])
        self.assertEqual(output['audit_scope'], 'selected_reviews')
        self.assertEqual(output['selected_review_ids'], ['1'])

    def test_selected_inventory_failure_closes_session_without_reading_database(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        session = MagicMock()
        with patch(target + 'login_session', return_value=session), \
                patch(target + 'live_inventory', side_effect=ValueError('Requested ID absent')), \
                patch(target + 'read_snapshot') as snapshot:
            with self.assertRaises(CommandError):
                Command(stdout=StringIO()).handle(report=None, review_ids='1,2')
        session.close.assert_called_once()
        snapshot.assert_not_called()

    def test_invalid_selected_ids_are_rejected_before_any_external_read(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        for raw in ('', ' ', '0', '-1', '01', '1.5', '1,', '1 or true',
                    ','.join(str(number) for number in range(1, 202))):
            with self.subTest(raw=raw), patch(target + 'login_session') as login, \
                    patch(target + 'read_snapshot') as snapshot:
                with self.assertRaises(CommandError):
                    Command(stdout=StringIO()).handle(report=None, review_ids=raw)
                login.assert_not_called()
                snapshot.assert_not_called()
        for selected in ([], ['0'], ['-1'], ['01'], ['1.5'], ['1 or true'],
                         list(range(1, 202)), '12'):
            with self.subTest(selected=selected), patch(target + 'connection') as database:
                with self.assertRaises(ValueError):
                    read_snapshot(aptem_review_ids=selected)
                database.close.assert_not_called()

    def test_selected_snapshot_sql_excludes_unrelated_parents_children_and_orphans(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        parent = (100, '1', 50, 'Completed', 'success', json.dumps(self.row['data']), 1)
        unrelated_parent = (200, '99', 51, 'Archived', 'partial', '{}', 0)
        section = (100, 1, 'Questions', 'verified', 'CustomContent', True,
                   True, True, True, True, json.dumps(self.payload),
                   json.dumps(self.fields), None, None)
        with patch(target + 'connection') as database, patch(target + 'transaction.atomic') as atomic:
            cursor = database.cursor.return_value.__enter__.return_value

            def selected_rows():
                sql, *arguments = cursor.execute.call_args.args
                params = arguments[0] if arguments else None
                if sql == 'SELECT id,aptem_id FROM "Learner".learners':
                    return [(50, 10), (51, 11)]
                if sql.startswith('SELECT id FROM "Learner".reviews'):
                    return [(row[0],) for row in (parent, unrelated_parent)
                            if params is None or row[1] in params[0]]
                if 'jsonb_build_object(' in sql and sql != SECTION_SNAPSHOT_SQL:
                    return [row for row in (parent, unrelated_parent) if row[0] in params[0]]
                if sql.startswith('SELECT DISTINCT review_id FROM'):
                    return [(pk,) for pk in (100, 200, 909) if params is None or pk in params[0]]
                if sql == SECTION_SNAPSHOT_SQL:
                    return [section] if 100 in params[0] else []
                if 'LEFT JOIN "Learner".reviews r' in sql:
                    return [(909,)] if params is None or 909 in params[0] else []
                raise AssertionError('Unexpected snapshot statement')

            cursor.fetchall.side_effect = selected_rows
            snapshot = read_snapshot(aptem_review_ids=['1'])
        queries = cursor.execute.call_args_list
        self.assertEqual(queries[0].args[0],
                         'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertEqual(queries[1].args, ('SELECT id,aptem_id FROM "Learner".learners',))
        self.assertIn('WHERE aptem_review_id=ANY(%s)', queries[2].args[0])
        self.assertEqual(queries[2].args[1], [['1']])
        child_query = next(call for call in queries if call.args[0].startswith('SELECT DISTINCT review_id FROM'))
        self.assertIn('WHERE review_id=ANY(%s)', child_query.args[0])
        self.assertEqual(child_query.args[1], [[100]])
        self.assertIn('AND s.review_id=ANY(%s)', queries[-1].args[0])
        self.assertEqual(queries[-1].args[1], [[100]])
        self.assertEqual([row['aptem_review_id'] for row in snapshot[1]], ['1'])
        self.assertEqual(set(snapshot[2]), {100})
        self.assertEqual(snapshot[3], [])
        self.assertEqual(snapshot[0], {50: 10, 51: 11})
        atomic.assert_called_once()
        result = audit_inventory([self.source], *snapshot)
        self.assertTrue(result['complete'], result['issues'])
        self.assertEqual(result['totals']['content_sections_checked'], 1)
        self.assertEqual(result['declared_answer_checks']['false_answers'], 1)

    def test_selected_snapshot_keeps_global_canonical_uniqueness_and_missing_review_checks(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        with patch(target + 'connection') as database, patch(target + 'transaction.atomic'):
            cursor = database.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[(50, 10), (51, 10)], [], [], []]
            snapshot = read_snapshot(aptem_review_ids=['1'])
        result = audit_inventory([self.source], *snapshot)
        self.assertFalse(result['complete'])
        self.assertEqual(result['issues']['missing_review'], ['1'])
        self.assertEqual(result['issues']['duplicate_canonical_aptem_learner_id'], ['10'])
        child_query = next(call for call in cursor.execute.call_args_list
                           if call.args[0].startswith('SELECT DISTINCT review_id FROM'))
        self.assertEqual(child_query.args[1], [[]])

    def test_default_command_keeps_full_inventory_and_snapshot_api(self):
        target = 'aptem.management.commands.audit_review_content_capture.'
        session = MagicMock()
        command = Command(stdout=StringIO())
        with patch(target + 'login_session', return_value=session), \
                patch(target + 'live_inventory', return_value=[self.source]) as inventory, \
                patch(target + 'read_snapshot', return_value=({50: 10}, [self.row],
                                                              {100: [self.section]}, [])) as snapshot:
            command.handle(report=None)
        inventory.assert_called_once_with(session)
        snapshot.assert_called_once_with()
        output = json.loads(command.stdout.getvalue())
        self.assertEqual(output['audit_scope'], 'all_reviews')
        self.assertIsNone(output['selected_review_ids'])
