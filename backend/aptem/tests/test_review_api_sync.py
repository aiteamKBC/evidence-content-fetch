import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from aptem.services.review_api_sync import (
    ReviewSyncError, display_status, fetch_review_content,
    fetch_review_content_from_template, needs_content, real_answers,
    CAPTURE_VERSION, _merge_preserved_sections, _payload, live_inventory,
    save_live_review, capture_is_verified, _fetch_meeting_summary,
    _fetch_previous_meeting_summary, _fetch_supplemental_sections,
    _fetch_actions, _fetch_plr, api_get, login_session, ACTION_ENDPOINT,
    _recover_review_type_guid, _review_type_light_read, _set_manifest_verification,
)
from aptem.services.aptem_auth import AptemLoginError


class ReviewApiSyncTests(SimpleTestCase):
    @staticmethod
    def response(payload):
        result = MagicMock()
        result.json.return_value = payload
        return result

    def test_display_status_matches_existing_lms_values(self):
        self.assertEqual(display_status('NotScheduled'), 'Not Scheduled')
        self.assertEqual(display_status('InProgress'), 'In Progress')
        self.assertEqual(display_status('Completed'), 'Completed')

    def test_only_corrected_verified_completed_capture_may_skip_refetch(self):
        review = {'Status': 'Completed'}
        existing = {'status': 'Completed', 'extraction_status': 'success', 'sections': 5,
                    'capture_version': CAPTURE_VERSION, 'capture_verified': True}
        self.assertFalse(needs_content(review, existing))
        existing['status'] = 'In Progress'
        self.assertTrue(needs_content(review, existing))
        existing.update(status='Completed', extraction_status='partial')
        self.assertTrue(needs_content(review, existing))
        existing.update(extraction_status='success', capture_version=4)
        self.assertTrue(needs_content(review, existing))
        existing.update(capture_version=CAPTURE_VERSION, capture_verified=False)
        self.assertTrue(needs_content(review, existing))

    def test_archived_content_is_required(self):
        self.assertTrue(needs_content({'Status': 'Archived'}, None))

    def test_corrected_completed_capture_is_refetched_when_source_changes(self):
        existing = {'status': 'Completed', 'extraction_status': 'success', 'sections': 2,
                    'capture_version': CAPTURE_VERSION, 'capture_verified': True,
                    'content_source_updated_at': '2026-10-01T01:00:00Z'}
        self.assertFalse(needs_content({'Status': 'Completed',
                                       'UpdatedDate': '2026-10-01T01:00:00Z'}, existing))
        self.assertTrue(needs_content({'Status': 'Completed',
                                      'UpdatedDate': '2026-10-01T02:00:00Z'}, existing))

    def test_mutable_started_reviews_are_refetched(self):
        existing = {'status': 'In Progress', 'extraction_status': 'success', 'sections': 5}
        self.assertTrue(needs_content({'Status':'InProgress'}, existing))
        self.assertTrue(needs_content({'Status':'AwaitingSignature'}, existing))
        self.assertFalse(needs_content({'Status':'Scheduled'}, None))

    def test_unknown_section_action_marks_capture_unavailable(self):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {'sections': [
            {'name':'New Aptem section','status':'Complete',
             'contentRoute':{'action':'UnknownContent'}},
        ]}
        with patch('aptem.services.review_api_sync.api_get', return_value=response):
            manifest, sections = fetch_review_content(session, '123')
        self.assertFalse(manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'unavailable')
        self.assertIn('Unmapped', sections[0]['capture_error'])

    def test_unknown_incomplete_section_is_not_proven_empty(self):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {'sections': [
            {'name':'Future section','status':'Incomplete',
             'contentRoute':{'action':'FutureContent'}},
        ]}
        with patch('aptem.services.review_api_sync.api_get', return_value=response):
            manifest, sections = fetch_review_content(session, '123')
        self.assertFalse(manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'unavailable')
        self.assertEqual(sections[0]['source_status'], 'Incomplete')

    @patch('aptem.services.review_api_sync._fetch_supplemental_sections', return_value=[])
    def test_incomplete_section_with_api_error_is_unavailable(self, supplemental):
        manifest = MagicMock()
        manifest.json.return_value = {'sections': [
            {'name':'Meeting Details','status':'Incomplete',
             'contentRoute':{'action':'MeetingDetailsContent'}},
        ]}
        extended = MagicMock()
        extended.json.return_value = {'associatedTaskId':44,'userId':88}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                manifest, extended, ReviewSyncError('Aptem HTTP 400')]):
            captured_manifest, sections = fetch_review_content(MagicMock(), '123')
        self.assertEqual(sections[0]['section_name'], 'Meeting Details')
        self.assertEqual(sections[0]['fields'], [])
        self.assertFalse(captured_manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'unavailable')

    @patch('aptem.services.review_api_sync._fetch_supplemental_sections', return_value=[])
    def test_completed_meeting_extended_record_does_not_prove_missing_content(self, supplemental):
        manifest = MagicMock()
        manifest.json.return_value = {'sections': [
            {'name':'Meeting Details','status':'Complete',
             'contentRoute':{'action':'MeetingDetailsContent'}},
        ]}
        extended = MagicMock()
        extended.json.return_value = {
            'associatedTaskId':44,'userId':88,'method':'Online',
            'plannedStartDateTime':'2026-01-02T10:00:00Z',
        }
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                manifest, extended, ReviewSyncError('Aptem HTTP 400')]):
            captured_manifest, sections = fetch_review_content(MagicMock(), '123')
        self.assertIn('Online', sections[0]['raw_text'])
        self.assertFalse(captured_manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'unavailable')

    @patch('aptem.services.review_api_sync._fetch_supplemental_sections', return_value=[])
    def test_exact_source_absence_keeps_response_proof_and_extended_metadata(self, supplemental):
        manifest = MagicMock()
        manifest.json.return_value = {'sections': [
            {'name': 'Meeting Details', 'status': 'Incomplete',
             'contentRoute': {'action': 'MeetingDetailsContent'}},
        ]}
        extended = MagicMock()
        extended.json.return_value = {'associatedTaskId': 44, 'userId': 88, 'method': 'Online'}
        proof = {'message': 'There is no corresponding Review meeting'}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                manifest, extended, ReviewSyncError('Aptem HTTP 400', status_code=400,
                                                   source_payload=proof)]):
            captured_manifest, sections = fetch_review_content(MagicMock(), '123')
        self.assertTrue(captured_manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'verified_source_absent')
        self.assertEqual(sections[0]['source_payload']['meeting_response'], proof)
        self.assertEqual(sections[0]['source_scope'], 'review_metadata_only')

    def test_five_worker_shards_are_disjoint_and_complete(self):
        ids = list(range(1,1001))
        shards = [[rid for rid in ids if rid % 5 == index] for index in range(5)]
        self.assertEqual(sum(map(len,shards)),len(ids))
        self.assertEqual(set().union(*map(set,shards)),set(ids))
        for index, shard in enumerate(shards):
            for other in shards[index+1:]:
                self.assertFalse(set(shard) & set(other))

    def test_prior_learning_collection_preserves_each_answer(self):
        answers = real_answers([{'id':18,'description':'Prior experience','impact':'High'}])
        self.assertEqual(answers, {
            'Item 1 - description':'Prior experience',
            'Item 1 - impact':'High',
        })

    def test_skill_radar_captures_assigned_competencies_and_progress(self):
        manifest_response = MagicMock()
        manifest_response.json.return_value = {
            'userId': 88,
            'sections': [{'name':'Skill Radar','status':'Complete',
                          'contentRoute':{'action':'SkillRadarContent'}}],
        }
        competencies_response = MagicMock()
        competencies_response.json.return_value = [{'id':7,'name':'Core Skills'}]
        progress_response = MagicMock()
        progress_response.json.return_value = [
            {'id':9,'name':'Communication','assessments':[{'level':2}]},
        ]
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                manifest_response, competencies_response, progress_response]) as mocked:
            _, sections = fetch_review_content(MagicMock(), '123')
        self.assertEqual(sections[0]['section_name'], 'Skill Radar')
        self.assertIn('Communication', sections[0]['raw_text'])
        self.assertEqual(mocked.call_args_list[1].kwargs['params'], {'userId':'88'})
        self.assertEqual(mocked.call_args_list[2].kwargs['params'],
                         {'competencyId':'7','userId':'88'})
        self.assertEqual(sections[0]['source_payload']['assigned_competencies'],
                         [{'id': 7, 'name': 'Core Skills'}])
        self.assertEqual(sections[0]['source_scope'], 'current_learner_competency_report')

    @patch('aptem.services.review_api_sync._fetch_section')
    @patch('aptem.services.review_api_sync.api_get')
    @patch('aptem.services.review_api_sync._recover_review_type_guid', return_value=(None, {'verified': False}))
    def test_broken_manifest_recovery_requires_same_internal_type(self, recover_guid, get, fetch):
        target = MagicMock(); target.json.return_value = {'id':9,'typeId':352,'userId':88}
        template_meta = MagicMock(); template_meta.json.return_value = {'id':8,'typeId':352}
        template = MagicMock(); template.json.return_value = {'sections': [
            {'name':'Custom','customName':'Questions','status':'Complete',
             'contentRoute':{'action':'CustomContent'}},
        ]}
        get.side_effect = [target, template_meta, template]
        fetch.return_value = {'section_name':'Questions','fields':[{'label':'A','value':'B'}],
                              'tables':[],'raw_text':'A\nB'}
        manifest, sections = fetch_review_content_from_template(MagicMock(),'9','8')
        self.assertTrue(manifest['manifest_reconstructed'])
        self.assertFalse(manifest['capture_verified'])
        self.assertEqual(manifest['sections'][0]['status'],'UnknownFromTemplate')
        self.assertEqual(sections[0]['section_name'],'Questions')

        target.json.return_value = {'id':9,'typeId':999}
        get.side_effect = [target, template_meta]
        with self.assertRaisesRegex(ReviewSyncError, 'does not match'):
            fetch_review_content_from_template(MagicMock(),'9','8')

    def test_declared_zero_false_metadata_titles_and_duplicate_labels_are_retained(self):
        payload = {
            'fields': [{'name': 'a', 'title': 'Name'}, {'name': 'b', 'title': 'Status'},
                       {'name': 'c', 'title': 'Same'}, {'name': 'd', 'title': 'Same'}],
            'a': 0, 'b': False, 'c': 'first', 'd': 'second',
        }
        self.assertEqual(real_answers(payload),
                         {'Name': 0, 'Status': False, 'Same': 'first', 'Same [2]': 'second'})

    def test_scalar_and_mixed_list_json_is_retained(self):
        self.assertEqual(real_answers(False), {'Value': False})
        self.assertEqual(real_answers([0, False, 'x', {'answer': 'y'}]),
                         {'Item 1 - Value': 0, 'Item 2 - Value': False,
                          'Item 3 - Value': 'x', 'Item 4 - answer': 'y'})

    def test_selectable_list_options_are_never_invented_as_an_answer(self):
        payload = {'fields': [
            {'name': 'missing', 'title': 'Missing', 'listAnswers': ['1', '2', '3']},
            {'name': 'zero', 'title': 'Zero', 'listAnswers': ['1', '2', '3']},
            {'name': 'false', 'title': 'False', 'listAnswers': ['Yes', 'No']},
        ], 'missing': None, 'zero': 0, 'false': False}
        self.assertEqual(real_answers(payload), {'Zero': 0, 'False': False})

    def test_original_section_json_is_preserved_even_when_rendered_fields_are_filtered(self):
        manifest = MagicMock()
        manifest.json.return_value = {'id': 123, 'sections': [
            {'name': 'Questions', 'status': 'Complete',
             'contentRoute': {'action': 'CustomContent'}},
        ]}
        response = MagicMock()
        response.headers = {'content-type': 'application/json'}
        payload = {'entityId': 77, 'fields': [{'name': 'value', 'title': 'Name'}],
                   'value': False, 'hidden_source_property': ['preserved']}
        response.json.return_value = payload
        with patch('aptem.services.review_api_sync.api_get', side_effect=[manifest, response]):
            captured_manifest, sections = fetch_review_content(MagicMock(), '123')
        self.assertTrue(captured_manifest['capture_verified'])
        self.assertEqual(sections[0]['source_payload'], payload)
        self.assertEqual(sections[0]['fields'], [{'label': 'Name', 'value': 'No'}])

    def test_manifest_wrong_identity_fails_before_section_requests(self):
        response = MagicMock()
        response.json.return_value = {'id': 999, 'sections': []}
        with patch('aptem.services.review_api_sync.api_get', return_value=response) as get:
            with self.assertRaisesRegex(ReviewSyncError, 'different review'):
                fetch_review_content(MagicMock(), '123')
        self.assertEqual(get.call_count, 1)

    def test_empty_explicit_manifest_is_distinct_from_missing_manifest(self):
        response = MagicMock()
        response.json.return_value = {'id': 123, 'sections': []}
        with patch('aptem.services.review_api_sync.api_get', return_value=response):
            manifest, sections = fetch_review_content(MagicMock(), '123')
            self.assertTrue(manifest['capture_verified'])
            self.assertEqual(sections, [])
            response.json.return_value = {'id': 123}
            with self.assertRaisesRegex(ReviewSyncError, 'manifest'):
                fetch_review_content(MagicMock(), '123')

    def test_metadata_only_does_not_promote_or_erase_raw_sql_json_capture(self):
        old = {'capture_version': 4, 'capture_source': 'browser', 'sections': [{'keep': True}]}
        actual = _payload({'Id': 123, 'LearnerId': 88}, old_data=json.dumps(old))
        self.assertEqual(actual['capture_version'], 4)
        self.assertEqual(actual['capture_source'], 'browser')
        self.assertEqual(actual['sections'], old['sections'])
        self.assertNotIn('capture_version', _payload({'Id': 123, 'LearnerId': 88}))

    def test_fresh_capture_clears_stale_failure_audit_but_metadata_keeps_it(self):
        review = {'Id': 123, 'LearnerId': 88}
        old = {'content_audit': {'verified': False, 'reason': 'Earlier HTTP 500'}}
        self.assertEqual(_payload(review, old_data=old)['content_audit'], old['content_audit'])
        updated = _payload(review, {'capture_verified': True, 'sections': []}, [], old)
        self.assertNotIn('content_audit', updated)
        self.assertTrue(updated['capture_verified'])

    def test_partial_capture_preserves_prior_fields_and_unmatched_sections(self):
        previous = [
            {'section_name': 'Q', 'fields': [{'label': 'X', 'value': 'Saved answer'}],
             'tables': [], 'raw_text': 'Saved answer', 'source_payload': {'answer': 'Saved answer'}},
            {'section_name': 'Extra', 'fields': [], 'tables': [], 'raw_text': 'Also saved'},
        ]
        failed = [{'section_name': 'Q', 'fields': [], 'tables': [], 'raw_text': '',
                   'capture_state': 'unavailable', 'source_payload': None}]
        merged = _merge_preserved_sections(failed, previous)
        self.assertEqual(merged[0]['raw_text'], 'Saved answer')
        self.assertEqual(merged[0]['capture_state'], 'unavailable')
        self.assertTrue(merged[0]['preserved_existing'])
        self.assertEqual(merged[0]['previous_source_payload'], {'answer': 'Saved answer'})
        self.assertEqual(merged[1]['raw_text'], 'Also saved')
        self.assertEqual(merged[1]['capture_state'], 'preserved_unverified')
        self.assertEqual(failed[0]['raw_text'], '')  # Caller capture remains unchanged.

    def test_repeated_failures_retain_last_verified_raw_payload_and_current_error(self):
        previous = [{'section_name': 'Q', 'fields': [{'label': 'A', 'value': 'yes'}],
                     'tables': [], 'raw_text': 'yes', 'capture_state': 'verified',
                     'source_payload': {'answer': 'yes'}}]
        first_failure = [{'section_name': 'Q', 'fields': [], 'tables': [], 'raw_text': '',
                          'capture_state': 'unavailable',
                          'source_payload': {'message': 'first error'}}]
        second_failure = [{**first_failure[0], 'source_payload': {'message': 'second error'}}]
        first = _merge_preserved_sections(first_failure, previous)
        second = _merge_preserved_sections(second_failure, first)
        self.assertEqual(second[0]['source_payload'], {'message': 'second error'})
        self.assertEqual(second[0]['previous_source_payload'], {'answer': 'yes'})
        self.assertEqual(second[0]['fields'], previous[0]['fields'])
        self.assertEqual(second[0]['capture_state'], 'unavailable')

    def test_verified_base_capture_cannot_discard_nonempty_auxiliary_history(self):
        source = [{'section_name': 'Q', 'fields': [{'label': 'A', 'value': 'fresh'}],
                   'tables': [], 'raw_text': 'fresh', 'capture_state': 'verified'}]
        previous = [{'section_name': 'Meeting Summary', 'fields': [], 'tables': [],
                     'raw_text': 'historical meeting summary'}]
        merged = _merge_preserved_sections(source, previous)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1]['raw_text'], 'historical meeting summary')
        self.assertEqual(merged[1]['source_kind'], 'historical_preserved')
        self.assertEqual(merged[1]['capture_state'], 'preserved_unverified')
        self.assertFalse(capture_is_verified({'capture_verified': True,
                                             'sections': [{'name': 'Q'}]}, merged))

    def test_only_provably_identical_extra_duplicates_are_dropped(self):
        section = {'section_name': 'Q', 'fields': [{'label': 'A', 'value': 'same'}],
                   'tables': [], 'raw_text': 'same', 'capture_state': 'verified'}
        different = {**section, 'raw_text': 'additional detail'}
        merged = _merge_preserved_sections([section], [section, dict(section), different])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1]['raw_text'], 'additional detail')

    def test_completeness_includes_explicit_supplemental_sources(self):
        manifest = {'capture_verified': True, 'sections': [{'name': 'Q'}],
                    'supplemental_sections': [{'section_name': 'Meeting Summary',
                                               'source_action': 'ReviewSummarySupplemental'}]}
        captures = [{'section_name': 'Q', 'capture_state': 'verified'},
                    {'section_name': 'Meeting Summary', 'capture_state': 'verified',
                     'source_kind': 'supplemental'}]
        self.assertTrue(capture_is_verified(manifest, captures))
        self.assertFalse(capture_is_verified(manifest, captures[:1]))

    @patch('aptem.services.review_api_sync.transaction.atomic')
    @patch('aptem.services.review_api_sync.connection')
    def test_successful_manifest_upsert_preserves_omitted_nonempty_db_section(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (77, 88, '{}')
        cursor.fetchall.return_value = [('Meeting Summary', '[]', '[]', 'previous summary')]
        manifest = {'id': 123, 'userId': 88, 'sections': [{'name': 'Q'}], 'capture_verified': True}
        sections = [{'section_name': 'Q', 'fields': [], 'tables': [], 'raw_text': '',
                     'capture_state': 'verified', 'source_payload': {}}]
        save_live_review({'Id': 123, 'LearnerId': 88}, 88, manifest, sections)
        update = next(call for call in cursor.execute.call_args_list
                      if 'SET review_name' in call.args[0])
        self.assertEqual(update.args[1][10], 'partial')
        data = json.loads(update.args[1][7])
        self.assertEqual(data['capture_version'], 6)
        self.assertFalse(data['capture_verified'])
        self.assertEqual(data['sections'][1]['raw_text'], 'previous summary')

    def test_supplemental_meeting_summary_preserves_summary_notes_and_file_metadata(self):
        summary, transcripts = MagicMock(), MagicMock()
        summary.json.return_value = {'ReviewId': 123, 'SummarisationText': '<p>Actual summary</p>'}
        transcripts.json.return_value = {'entityId': 123, 'meetingNotes': 'Notes',
                                         'files': [{'id': 'file-id', 'fileName': 'transcript.txt'}]}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[summary, transcripts]):
            section = _fetch_meeting_summary(MagicMock(), '123')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_kind'], 'supplemental')
        self.assertEqual(section['source_payload']['summary'], summary.json.return_value)
        self.assertEqual(section['source_payload']['transcripts'], transcripts.json.return_value)
        self.assertEqual([field['label'] for field in section['fields']],
                         ['Summary', 'Meeting Notes', 'Transcripts'])

    def test_documented_empty_transcript_200_is_recorded_with_absence_proof(self):
        summary, transcripts = MagicMock(), MagicMock()
        summary.json.return_value = {'ReviewId': 123, 'SummarisationText': ''}
        transcripts.status_code = 200
        transcripts.content = b''
        with patch('aptem.services.review_api_sync.api_get', side_effect=[summary, transcripts]):
            section = _fetch_meeting_summary(MagicMock(), '123')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['fields'], [])
        self.assertEqual(section['source_payload']['transcripts_response'],
                         {'http_status': 200, 'empty_body': True})
        transcripts.json.assert_not_called()

    def test_missing_summary_404_does_not_become_verified_empty(self):
        transcripts = MagicMock()
        transcripts.json.return_value = {'entityId': 123, 'meetingNotes': 'Still readable', 'files': []}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                ReviewSyncError('HTTP 404', status_code=404), transcripts]):
            section = _fetch_meeting_summary(MagicMock(), '123')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertIn('404', section['capture_error'])
        self.assertIn('Still readable', section['raw_text'])

    def test_summary_identity_mismatch_cannot_be_verified(self):
        summary, transcripts = MagicMock(), MagicMock()
        summary.json.return_value = {'ReviewId': 999, 'SummarisationText': 'Wrong review'}
        transcripts.json.return_value = None
        with patch('aptem.services.review_api_sync.api_get', side_effect=[summary, transcripts]):
            section = _fetch_meeting_summary(MagicMock(), '123')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertNotIn('Wrong review', section['raw_text'])

    def test_previous_summary_matches_ui_type_date_and_id_selection(self):
        selector, summary = MagicMock(), MagicMock()
        selector.json.return_value = [
            {'id': 100, 'typeGuid': 'same', 'completeDateTime': '2026-01-01T00:00:00Z'},
            {'id': 105, 'typeGuid': 'other', 'completeDateTime': '2026-02-01T00:00:00Z'},
            {'id': 101, 'typeGuid': 'same', 'completeDateTime': '2026-01-01T00:00:00Z'},
        ]
        summary.json.return_value = {'ReviewId': 101, 'SummarisationText': 'Selected previous summary'}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[selector, summary]) as get:
            section = _fetch_previous_meeting_summary(MagicMock(), '123', {'typeGuid': 'same'})
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_payload']['selected_review_id'], '101')
        self.assertEqual(get.call_args_list[1].args[1],
                         '/api/Aptem.Review.Summarisation/ReviewSummaries/101')

    def test_no_previous_matching_review_is_authoritative_empty(self):
        selector = MagicMock()
        selector.json.return_value = []
        with patch('aptem.services.review_api_sync.api_get', return_value=selector) as get:
            section = _fetch_previous_meeting_summary(MagicMock(), '123', {'typeGuid': 'same'})
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['fields'], [])
        self.assertIsNone(section['source_payload']['selected_review_id'])
        self.assertEqual(get.call_count, 1)

    def test_previous_summary_missing_type_guid_is_unavailable_not_inferred(self):
        selector = MagicMock()
        selector.json.return_value = [{'id': 100, 'typeGuid': 'other', 'completeDateTime': '2026-01-01'}]
        with patch('aptem.services.review_api_sync.api_get', return_value=selector):
            section = _fetch_previous_meeting_summary(MagicMock(), '123', {})
        self.assertEqual(section['capture_state'], 'unavailable')

    def test_no_meeting_manifest_does_not_fetch_inapplicable_auxiliary_endpoints(self):
        manifest = {'sections': [{'name': 'Questions', 'contentRoute': {'action': 'CustomContent'}}]}
        with patch('aptem.services.review_api_sync.api_get') as get:
            self.assertEqual(_fetch_supplemental_sections(MagicMock(), '123', manifest), [])
        get.assert_not_called()

    def test_verified_rag_source_replaces_both_documented_ui_aliases(self):
        previous = [{'section_name': 'RAG Status', 'fields': [], 'tables': [], 'raw_text': 'UI label'},
                    {'section_name': 'RAG', 'fields': [], 'tables': [], 'raw_text': 'Old API rendering'}]
        source = [{'section_name': 'RAG', 'fields': [], 'tables': [], 'raw_text': 'Current API',
                   'source_action': 'RagContent', 'capture_state': 'verified'}]
        self.assertEqual(_merge_preserved_sections(source, previous), source)
        source[0]['capture_state'] = 'unavailable'
        self.assertEqual(len(_merge_preserved_sections(source, previous)), 2)
        self.assertEqual(real_answers({'level': 0, 'currentRagLevel': 2}),
                         {'RAG Level': 0, 'currentRagLevel': 2})

    @patch('aptem.services.review_api_sync.transaction.atomic')
    @patch('aptem.services.review_api_sync.connection')
    def test_section_batch_insert_preserves_order_unicode_and_quoted_text(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (77, 88, '{}')
        cursor.fetchall.return_value = []
        names = ['Question one', 'Question two']
        texts = ["A learner's answer", 'إجابة محفوظة']
        sections = [{'section_name': name, 'fields': [{'label': 'Answer', 'value': text}],
                     'tables': [], 'raw_text': text, 'capture_state': 'verified',
                     'source_payload': {'answer': text}}
                    for name, text in zip(names, texts)]
        manifest = {'id': 123, 'userId': 88, 'capture_verified': True,
                    'sections': [{'name': name} for name in names]}
        save_live_review({'Id': 123, 'LearnerId': 88}, 88, manifest, sections)
        inserts = [call for call in cursor.execute.call_args_list
                   if 'INSERT INTO "Learner".review_sections' in call.args[0]]
        self.assertEqual(len(inserts), 1)
        sql, parameters = inserts[0].args
        self.assertEqual(sql.count('(%s,%s,%s,%s::jsonb,%s::jsonb,%s,now(),now())'), 2)
        self.assertEqual(parameters[0:3], [77, names[0], 1])
        self.assertEqual(parameters[6:9], [77, names[1], 2])
        self.assertEqual(parameters[5], texts[0])
        self.assertEqual(parameters[11], texts[1])
        self.assertEqual(json.loads(parameters[9])[0]['value'], texts[1])
        self.assertNotIn(texts[0], sql)

    @patch('aptem.services.review_api_sync.transaction.atomic')
    @patch('aptem.services.review_api_sync.connection')
    def test_empty_verified_capture_skips_batch_insert(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (77, 88, '{}')
        cursor.fetchall.return_value = []
        save_live_review({'Id': 123, 'LearnerId': 88}, 88,
                         {'id': 123, 'userId': 88, 'capture_verified': True, 'sections': []}, [])
        self.assertFalse(any('INSERT INTO "Learner".review_sections' in call.args[0]
                             for call in cursor.execute.call_args_list))

    def test_actions_use_current_programme_when_review_programme_differs(self):
        identity = {'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 111, 'Status': 'InProgress'}]}
        current_user = {'value': [{'Id': 88, 'UserProgram_ProgramId': 222}]}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response(identity), self.response(current_user), self.response([])]) as get:
            section = _fetch_actions(MagicMock(), '123', {'status': 'Incomplete'},
                                     {'userId': 88}, 'Actions')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_scope'], 'current_learner_programme')
        self.assertEqual(get.call_args_list[2].args[1],
                         '/actions/api/v2/learners/88/programmes/222/actions')
        self.assertEqual(section['source_payload']['review_identity'], identity)
        self.assertEqual(section['source_payload']['current_learner_programme'], current_user)

    def test_completed_actions_decode_and_preserve_original_snapshot_string(self):
        raw = '[{"description":"Saved action","status":0}]'
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 111,
                                         'Status': 'Completed'}]}),
                self.response({'value': [{'Id': 88, 'UserProgram_ProgramId': 222}]}),
                self.response(raw)]) as get:
            section = _fetch_actions(MagicMock(), '123', {'status': 'Complete'},
                                     {'userId': 88}, 'Actions')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_scope'], 'review_snapshot')
        self.assertTrue(section['source_snapshot_available'])
        self.assertEqual(section['source_payload']['snapshot'], raw)
        self.assertIn('Saved action', section['raw_text'])
        self.assertEqual(get.call_args_list[2].args[1],
                         '/actions/api/v2/learners/88/programmes/222/snapshots/123')
        self.assertEqual(get.call_count, 3)

    def test_absent_action_snapshot_falls_back_to_current_scope_explicitly(self):
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 111,
                                         'Status': 'AwaitingSignature'}]}),
                self.response({'value': [{'Id': 88, 'UserProgram_ProgramId': 222}]}),
                self.response(''), self.response([{'description': 'Current action'}])]):
            section = _fetch_actions(MagicMock(), '123', {'status': 'Complete'},
                                     {'userId': 88}, 'Actions')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_scope'], 'current_learner_programme_fallback')
        self.assertFalse(section['source_snapshot_available'])
        self.assertEqual(section['source_payload']['snapshot'], '')

    def test_action_snapshot_error_is_not_silently_replaced_with_current_actions(self):
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 111,
                                         'Status': 'Completed'}]}),
                self.response({'value': [{'Id': 88, 'UserProgram_ProgramId': 222}]}),
                ReviewSyncError('HTTP 500')]) as get:
            section = _fetch_actions(MagicMock(), '123', {'status': 'Complete'},
                                     {'userId': 88}, 'Actions')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertEqual(get.call_count, 3)

    def test_actions_identity_mismatch_stops_before_reading_other_learner_actions(self):
        with patch('aptem.services.review_api_sync.api_get', return_value=self.response(
                {'value': [{'Id': 123, 'LearnerId': 999, 'ProgramId': 111,
                            'Status': 'InProgress'}]})) as get:
            section = _fetch_actions(MagicMock(), '123', {}, {'userId': 88}, 'Actions')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertEqual(get.call_count, 1)

    @patch('aptem.services.review_api_sync.requests.Session')
    @patch('aptem.services.review_api_sync.login_to_aptem')
    def test_login_error_is_sanitized_and_session_closed(self, login, session):
        login.side_effect = AptemLoginError('Potentially sensitive upstream detail')
        with self.assertRaises(ReviewSyncError) as raised:
            login_session()
        self.assertEqual(str(raised.exception), 'Aptem authentication failed')
        session.return_value.close.assert_called_once()

    @patch('aptem.services.review_api_sync.login_to_aptem')
    def test_reauthentication_error_is_normalized_for_partial_section_capture(self, login):
        session = MagicMock()
        session.get.return_value.status_code = 403
        login.side_effect = AptemLoginError('Potentially sensitive upstream detail')
        with self.assertRaises(ReviewSyncError) as raised:
            api_get(session, '/source/path?token=secret#private')
        self.assertEqual(str(raised.exception),
                         'Aptem HTTP 403 for /source/path; re-authentication failed')
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.diagnostics, {
            'method': 'GET', 'path': '/source/path', 'status_code': 403,
            'attempts': 1, 'reauthentication': 'failed',
        })
        self.assertIsNone(raised.exception.source_payload)
        self.assertEqual(session.get.call_count, 1)

    @patch('aptem.services.review_api_sync.login_to_aptem')
    def test_unsuccessful_reauthentication_retains_status_for_get_and_type_query(self, login):
        login.return_value.login_success = False
        for operation in ('get', 'post'):
            with self.subTest(operation=operation):
                session = MagicMock()
                getattr(session, operation).return_value.status_code = 401
                with self.assertRaises(ReviewSyncError) as raised:
                    if operation == 'get':
                        api_get(session, '/source/path', params={'secret': 'not-diagnostic'})
                    else:
                        _review_type_light_read(session, {'secret': 'not-diagnostic'})
                error = raised.exception
                self.assertEqual(error.status_code, 401)
                self.assertEqual(error.diagnostics['method'], operation.upper())
                self.assertEqual(error.diagnostics['reauthentication'], 'failed')
                self.assertIn(error.diagnostics['path'], str(error))
                self.assertNotIn('not-diagnostic', json.dumps(error.diagnostics))
                self.assertEqual(getattr(session, operation).call_count, 1)

    @patch('aptem.services.review_api_sync.time.sleep')
    def test_exhausted_temporary_responses_keep_status_path_and_attempts(self, sleep):
        for operation in ('get', 'post'):
            with self.subTest(operation=operation):
                session = MagicMock()
                response = getattr(session, operation).return_value
                response.status_code = 500
                response.text = 'Private upstream body'
                response.headers = {'Set-Cookie': 'private-cookie'}
                with self.assertRaises(ReviewSyncError) as raised:
                    if operation == 'get':
                        api_get(session, '/api/MWS.Review/ReviewContent/123?token=private-token')
                    else:
                        _review_type_light_read(session, {'secret': 'private-token'})
                error = raised.exception
                self.assertEqual(error.status_code, 500)
                self.assertEqual(error.diagnostics['attempts'], 3)
                self.assertEqual(error.diagnostics['method'], operation.upper())
                self.assertIn(error.diagnostics['path'], str(error))
                self.assertEqual(getattr(session, operation).call_count, 3)
                self.assertIsNone(error.source_payload)
                response.json.assert_not_called()
                diagnostics = str(error) + json.dumps(error.diagnostics)
                for forbidden in ('private-token', 'Private upstream body', 'private-cookie'):
                    self.assertNotIn(forbidden, diagnostics)

    @patch('aptem.services.review_api_sync.login_to_aptem')
    def test_forbidden_after_successful_refresh_remains_unavailable_with_safe_provenance(self, login):
        login.return_value.login_success = True
        session = MagicMock()
        response = session.get.return_value
        response.status_code = 403
        response.ok = False
        response.json.return_value = {'message': 'Source failure proof'}
        with self.assertRaises(ReviewSyncError) as raised:
            api_get(session, '/transcripts/api/v2/review/123')
        error = raised.exception
        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.diagnostics['reauthentication'], 'succeeded')
        self.assertEqual(error.source_payload, {'message': 'Source failure proof'})
        self.assertNotIn('Source failure proof', json.dumps(error.diagnostics))
        self.assertEqual(session.get.call_count, 2)
        login.assert_called_once()

    @patch('aptem.services.review_api_sync.login_to_aptem')
    def test_transcript_auth_failure_keeps_summary_and_structured_manifest_evidence(self, login):
        login.side_effect = AptemLoginError('Private login response')
        session = MagicMock()
        summary = self.response({'ReviewId': 123, 'SummarisationText': 'Saved summary'})
        summary.status_code, summary.ok = 200, True
        forbidden = MagicMock(status_code=403)
        session.get.side_effect = [summary, forbidden]
        section = _fetch_meeting_summary(session, '123')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertEqual(section['source_payload']['summary']['SummarisationText'], 'Saved summary')
        self.assertEqual(section['capture_failures'], [{
            'method': 'GET', 'path': '/transcripts/api/v2/review/123',
            'status_code': 403, 'attempts': 1, 'reauthentication': 'failed',
            'component': 'transcripts',
        }])
        manifest = {}
        _set_manifest_verification(manifest, [section])
        self.assertFalse(manifest['capture_verified'])
        self.assertEqual(manifest['capture_errors'][0]['capture_failures'], section['capture_failures'])
        self.assertNotIn('Private login response', json.dumps(manifest))

    @patch('aptem.services.review_api_sync.time.sleep')
    def test_core_section_temporary_error_is_structured_without_losing_manifest(self, sleep):
        session = MagicMock()
        manifest_response = self.response({'id': 123, 'sections': [{
            'name': 'Notes', 'contentRoute': {'action': 'CommentsContent'},
        }]})
        manifest_response.status_code, manifest_response.ok = 200, True
        failed = MagicMock(status_code=500)
        session.get.side_effect = [manifest_response, failed, failed, failed]
        manifest, sections = fetch_review_content(session, '123')
        self.assertFalse(manifest['capture_verified'])
        self.assertEqual(sections[0]['capture_state'], 'unavailable')
        failure = manifest['capture_errors'][0]['capture_failures'][0]
        self.assertEqual(failure, {'method': 'GET', 'path': '/api/MWS.Review/Comments/123',
                                   'status_code': 500, 'attempts': 3})

    def schema_duplicate_example(self):
        source = {'section_name': 'Opening', 'fields': [], 'tables': [], 'raw_text': '',
                  'source_action': 'CustomContent', 'capture_state': 'verified',
                  'source_payload': {
                      'fields': [{'name': 'one', 'title': 'Question &amp; detail',
                                  'description': '<p>Explain the <strong>next step</strong>.</p>'},
                                 {'name': 'two', 'title': 'Second question',
                                  'description': '<p>Another instruction.</p>'}],
                      'one': None, 'two': '',
                  }}
        old = {'section_name': 'Opening', 'fields': [
            {'label': 'Question & detail', 'description': 'Explain the next step.', 'value': ''},
            {'label': 'Second question', 'description': 'Another instruction.', 'value': None},
        ], 'tables': [],
            'raw_text': 'Question & detail\nExplain the next step.\n\nSecond question\nAnother instruction.'}
        empty = {'section_name': 'Opening', 'fields': [], 'tables': [], 'raw_text': ''}
        return source, old, empty

    def test_legacy_schema_only_duplicate_is_proven_by_complete_original_source(self):
        source, old, empty = self.schema_duplicate_example()
        self.assertEqual(_merge_preserved_sections([source], [empty, old]), [source])

    def test_schema_duplicate_rule_never_discards_real_answer_or_unexplained_text(self):
        source, original, empty = self.schema_duplicate_example()
        changes = [
            lambda old: old['fields'][0].update(value='Actual old answer'),
            lambda old: old['fields'][0].update(value=False),
            lambda old: old['fields'][0].update(value=0),
            lambda old: old.update(raw_text=old['raw_text'] + '\nAdditional old note'),
            lambda old: old['fields'][0].update(description='Different old instruction'),
            lambda old: old['fields'][0].update(unmapped='Unaccounted field data'),
            lambda old: old.update(source_payload={'old_answer': 'Must retain'}),
        ]
        for change in changes:
            with self.subTest(change=change):
                old = deepcopy(original)
                change(old)
                merged = _merge_preserved_sections([source], [empty, old])
                self.assertEqual(len(merged), 2)
                self.assertEqual(merged[-1]['capture_state'], 'preserved_unverified')

    def test_schema_duplicate_requires_every_source_answer_empty_and_verified(self):
        original, old, empty = self.schema_duplicate_example()
        for change in (lambda source: source['source_payload'].update(one=False),
                       lambda source: source.update(capture_state='unavailable')):
            source = deepcopy(original)
            change(source)
            self.assertEqual(len(_merge_preserved_sections([source], [empty, old])), 2)

    def plan_reviews_duplicate_example(self):
        payload = [{'name': f'Review {i}', 'reviewType': 'Progress Review',
                    'plannedDate': '2026-10-01T00:00:00Z', 'actualDate': None}
                   for i in range(1, 8)]
        header = ['Review Name / Review Type', 'Planned Date', 'Actual Date']
        rows = [[f'Review {i} Progress Review', '01/10/2026', ''] for i in range(1, 6)]
        raw = '\n'.join(header + [cell for row in rows for cell in row] +
                        ['1 2 5 items per page 1 - 5 of 7 items'])
        old = {'section_name': 'Reviews Schedule', 'fields': [],
               'tables': [{'rows': [header]}, {'rows': rows}], 'raw_text': raw}
        source = {'section_name': 'Plan Reviews', 'fields': [], 'tables': [],
                  'raw_text': 'All seven source reviews', 'source_action': 'PlanReviewsContent',
                  'capture_state': 'verified', 'source_payload': payload}
        return source, old

    def test_reviews_schedule_alias_duplicate_requires_exact_first_page_and_pager_proof(self):
        source, old = self.plan_reviews_duplicate_example()
        self.assertEqual(_merge_preserved_sections([source], [old]), [source])
        self.assertEqual(len(source['source_payload']), 7)

    def test_schedule_alias_never_discards_unexplained_cells_notes_or_unverified_source(self):
        source, original = self.plan_reviews_duplicate_example()
        changes = [
            lambda old: old['tables'][1]['rows'][0].__setitem__(0, 'Different review'),
            lambda old: old.update(raw_text=old['raw_text'] + '\nUnexplained note'),
            lambda old: old.update(raw_text=old['raw_text'].replace('of 7 items', 'of 8 items')),
            lambda old: old['tables'][0].update(unknown='Unaccounted content'),
            lambda old: old.update(fields=[{'label': 'Answer', 'value': 'Keep'}]),
        ]
        for change in changes:
            old = deepcopy(original)
            change(old)
            merged = _merge_preserved_sections([source], [old])
            self.assertEqual(len(merged), 2)
            self.assertEqual(merged[-1]['capture_state'], 'preserved_unverified')
        source['capture_state'] = 'unavailable'
        self.assertEqual(len(_merge_preserved_sections([source], [original])), 2)

    def test_schedule_allows_only_actual_date_to_fill_a_previously_blank_cell(self):
        source, old = self.plan_reviews_duplicate_example()
        source['source_payload'][0]['actualDate'] = '2026-10-02T00:00:00Z'
        self.assertEqual(_merge_preserved_sections([source], [old]), [source])

        changed_actual = deepcopy(old)
        changed_actual['tables'][1]['rows'][0][2] = '03/10/2026'
        self.assertEqual(len(_merge_preserved_sections([source], [changed_actual])), 2)
        changed_planned = deepcopy(old)
        changed_planned['tables'][1]['rows'][0][1] = ''
        self.assertEqual(len(_merge_preserved_sections([source], [changed_planned])), 2)
        unexplained_note = {**old, 'raw_text': old['raw_text'] + '\nNew content'}
        self.assertEqual(len(_merge_preserved_sections([source], [unexplained_note])), 2)

    def test_exact_learning_progress_placeholder_requires_authoritative_empty_conditions(self):
        source = {'section_name': 'Learning progress', 'fields': [{'label': 'hasPrevProgress', 'value': 'No'}],
                  'tables': [], 'raw_text': 'hasPrevProgress\nNo', 'capture_state': 'verified',
                  'source_action': 'LearningProgressContent',
                  'source_payload': {'progress': [], 'hasPrevProgress': False}}
        placeholder = {'section_name': 'Learning Progress', 'fields': [], 'tables': [],
                       'raw_text': 'No information entered.'}
        # One ordinary matching row plus an extra legacy placeholder.
        self.assertEqual(_merge_preserved_sections([source], [source, placeholder]), [source])
        for change in (
                lambda row: row.update(raw_text='No information entered. Additional detail'),
                lambda row: row.update(fields=[{'label': 'Answer', 'value': 'Keep'}]),
                lambda row: row.update(source_payload={'legacy_content': 'Keep'})):
            old = deepcopy(placeholder)
            change(old)
            self.assertEqual(len(_merge_preserved_sections([source], [source, old])), 2)
        for change in (
                lambda row: row['source_payload'].update(progress=[{'value': 'New progress'}]),
                lambda row: row['source_payload'].update(hasPrevProgress=True),
                lambda row: row.update(capture_state='unavailable')):
            current = deepcopy(source)
            change(current)
            self.assertEqual(len(_merge_preserved_sections([current], [source, placeholder])), 2)

    def skill_radar_duplicate_example(self):
        levels = [{'characteristicsLevelId': 10, 'level': 1},
                  {'characteristicsLevelId': 20, 'level': 2}]
        characteristics = [
            {'name': 'First skill', 'levels': levels,
             'actions': [{'dateToComplete': '2026-10-01', 'goalCharacteristicsLevel': {'level': 2}}],
             'assessments': [
                 {'assessmentType': 'User', 'completedDate': '2026-10-03', 'characteristicsLevelId': 20},
                 {'assessmentType': 'Admin', 'completedDate': '2026-10-02', 'characteristicsLevelId': 10},
             ]},
            {'name': 'Second skill', 'levels': levels,
             'actions': [{'dateToComplete': '2026-09-01', 'goalCharacteristicsLevel': {'level': 2}},
                         {'dateToComplete': '2026-10-01', 'goalCharacteristicsLevel': {'level': 1}}],
             'assessments': [{'assessmentType': 'Admin', 'completedDate': '2026-10-02',
                              'characteristicsLevelId': 10}]},
            {'name': 'Third skill', 'levels': levels, 'actions': [], 'assessments': []},
        ]
        payload = {'assigned_competencies': [{'id': 1, 'name': 'Other competency'},
                                            {'id': 2, 'name': 'Selected competency'}],
                   'competency_progress': [
                       {'competency': {'id': 1, 'name': 'Other competency'}, 'characteristics': []},
                       {'competency': {'id': 2, 'name': 'Selected competency'},
                        'characteristics': characteristics},
                   ]}
        raw = '\n'.join(['Selected competency', 'Goal', 'Skills Coach assessment', 'Learner assessment',
                         'Show History', '1', 'First skill', 'Working Toward', '0', '1', '2',
                         '2', 'Second skill', 'Achieved', '0', '1', '2',
                         '3', 'Third skill', 'No Goal Set'])
        previous = {'section_name': 'Skills Radar',
                    'fields': [{'type': 'checkbox', 'label': 'Show History', 'value': 'on', 'checked': False}],
                    'tables': [], 'raw_text': raw}
        current = {'section_name': 'Skill Radar', 'fields': [], 'tables': [], 'raw_text': 'Full source history',
                   'source_action': 'SkillRadarContent', 'capture_state': 'verified', 'source_payload': payload}
        return current, previous

    def test_skill_radar_duplicate_requires_exact_selected_competency_ui_projection(self):
        current, previous = self.skill_radar_duplicate_example()
        self.assertEqual(_merge_preserved_sections([current], [previous]), [current])
        self.assertEqual(len(current['source_payload']['competency_progress']), 2)

    def test_skill_radar_guard_rejects_history_toggle_extra_scores_or_any_unexplained_fields(self):
        current, original = self.skill_radar_duplicate_example()
        changes = [
            lambda old: old['fields'][0].update(checked=True),
            lambda old: old['fields'][0].update(checked=0),
            lambda old: old['fields'][0].update(extra='Unaccounted data'),
            lambda old: old['fields'].append({'label': 'Old score', 'value': '99'}),
            lambda old: old.update(raw_text=old['raw_text'] + '\n99'),
            lambda old: old.update(raw_text=old['raw_text'].replace('Working Toward', 'Achieved')),
            lambda old: old.update(source_payload={'other_score': 99}),
        ]
        for change in changes:
            previous = deepcopy(original)
            change(previous)
            self.assertEqual(len(_merge_preserved_sections([current], [previous])), 2)

    def test_skill_radar_guard_rejects_unknown_roles_dates_ambiguous_selection_and_failed_capture(self):
        original, previous = self.skill_radar_duplicate_example()
        changes = [
            lambda source: source.update(capture_state='unavailable'),
            lambda source: source['source_payload']['competency_progress'][1]['characteristics'][0]['assessments'][0].update(assessmentType='UnknownRole'),
            lambda source: source['source_payload']['competency_progress'][1]['characteristics'][0]['assessments'][0].update(completedDate='invalid date'),
            lambda source: source['source_payload']['competency_progress'].append(
                deepcopy(source['source_payload']['competency_progress'][1])),
        ]
        for change in changes:
            current = deepcopy(original)
            change(current)
            self.assertEqual(len(_merge_preserved_sections([current], [previous])), 2)

    def test_skill_radar_employer_legend_requires_exact_proven_tenant_label(self):
        current, previous = self.skill_radar_duplicate_example()
        current['source_payload']['competency_progress'][1]['characteristics'][0]['assessments'][0]['assessmentType'] = 'Employer'
        previous['raw_text'] = previous['raw_text'].replace('Learner assessment', 'Employer assessment')
        self.assertEqual(_merge_preserved_sections([current], [previous]), [current])
        changed_label = {**previous, 'raw_text': previous['raw_text'].replace(
            'Employer assessment', 'Company assessment')}
        self.assertEqual(len(_merge_preserved_sections([current], [changed_label])), 2)
        changed_score = {**previous, 'raw_text': previous['raw_text'] + '\n99'}
        self.assertEqual(len(_merge_preserved_sections([current], [changed_score])), 2)

    def test_equal_renderings_do_not_discard_distinct_retained_original_payloads(self):
        fresh = {'section_name': 'Q', 'fields': [{'label': 'A', 'value': 'same'}],
                 'tables': [], 'raw_text': 'same', 'source_action': 'CustomContent',
                 'capture_state': 'verified', 'source_payload': {'answer': 'same'}}
        for key in ('source_payload', 'previous_source_payload'):
            old = deepcopy(fresh)
            old.update(capture_state='preserved_unverified')
            old[key] = {'answer': 'same', 'extra_original_answer': 'Keep this'}
            merged = _merge_preserved_sections([fresh], [fresh, old])
            self.assertEqual(len(merged), 2)
            self.assertEqual(merged[1][key], old[key])
            self.assertEqual(merged[1]['capture_state'], 'preserved_unverified')
            reordered = _merge_preserved_sections([fresh], [old, fresh])
            self.assertEqual(reordered, merged)

    def test_rag_alias_never_consumes_an_explicitly_different_section_action(self):
        fresh = {'section_name': 'RAG', 'fields': [], 'tables': [], 'raw_text': 'Current RAG',
                 'source_action': 'RagContent', 'capture_state': 'verified',
                 'source_payload': {'level': 2}}
        for name in ('RAG Status', 'RAG'):
            old = {'section_name': name, 'fields': [{'label': 'Unique question', 'value': 'Keep'}],
                   'tables': [], 'raw_text': 'Unique question Keep', 'source_action': 'CustomContent',
                   'source_payload': {'unique_question': 'Keep'}}
            merged = _merge_preserved_sections([fresh], [old])
            self.assertEqual(len(merged), 2)
            self.assertEqual(merged[1]['source_payload'], old['source_payload'])
        old_alias = {'section_name': 'RAG Status', 'fields': [], 'tables': [], 'raw_text': 'Old details',
                     'source_action': 'RagContent', 'source_payload': {'level': 2, 'extra': 'Keep'}}
        self.assertEqual(len(_merge_preserved_sections([fresh], [old_alias])), 2)

    def test_same_name_matching_and_generic_dedup_preserve_explicit_action_conflicts(self):
        fresh = {'section_name': 'Shared label', 'fields': [], 'tables': [], 'raw_text': 'same',
                 'source_action': 'CustomContent', 'capture_state': 'verified', 'source_payload': None}
        old = {**fresh, 'source_action': 'OtherContent'}
        self.assertEqual(len(_merge_preserved_sections([fresh], [old])), 2)
        fresh['source_action'] = None
        self.assertEqual(len(_merge_preserved_sections([fresh], [old])), 2)
        schema, old_schema, empty = self.schema_duplicate_example()
        old_schema['source_action'] = 'OtherContent'
        self.assertEqual(len(_merge_preserved_sections([schema], [empty, old_schema])), 2)

    def plr_source_example(self):
        identity = {'value': [{'Id': 88, 'UserILRSummary_ULN': None}]}
        records = [{'id': 10, 'userId': 88, 'qualificationType': 'Example', 'grade': 'Pass'}]
        attainment = {'userId': 88, 'englishLevel': None, 'mathsLevel': None, 'ictLevel': None,
                      'selectedHighestLevel': 0, 'learnerSelectedHighestLevel': 2,
                      'isIlrUserFieldsReadonly': False}
        return identity, records, attainment

    def test_plr_uses_learner_qualification_attainment_and_uln_sources_not_prior_learning(self):
        identity, records, attainment = self.plr_source_example()
        manifest = {'id': 123, 'userId': 88, 'sections': [
            {'name': 'PLR', 'status': 'Complete', 'contentRoute': {'action': 'PLRContent'}},
        ]}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response(manifest), self.response(identity), self.response(records),
                self.response(attainment)]) as get:
            captured_manifest, sections = fetch_review_content(MagicMock(), '123')
        self.assertNotIn('PLRContent', ACTION_ENDPOINT)
        self.assertTrue(captured_manifest['capture_verified'])
        self.assertEqual(sections[0]['source_contract'], 'plr-ui-v1')
        self.assertEqual(sections[0]['source_scope'], 'current_learner_plr')
        self.assertEqual(sections[0]['source_payload'], {
            'learner_identity': identity, 'qualifications': records, 'prior_attainment': attainment})
        paths = [call.args[1] for call in get.call_args_list]
        self.assertEqual(paths, ['/api/MWS.Review/ReviewContent/123', '/odata/1.0/Users',
                                 '/api/Users/88/PLR', '/api/Users/88/PLR/PriorAttainmentLevel'])
        fields = {field['label']: field['value'] for field in sections[0]['fields']}
        self.assertEqual(fields['Prior attainment - selectedHighestLevel'], '0')
        self.assertEqual(fields['Prior attainment - isIlrUserFieldsReadonly'], 'No')

    def test_empty_qualifications_are_verified_only_with_correct_other_plr_sources(self):
        identity, _, attainment = self.plr_source_example()
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response(identity), self.response([]), self.response(attainment)]):
            section = _fetch_plr(MagicMock(), {}, {'userId': 88}, 'PLR')
        self.assertEqual(section['capture_state'], 'verified')
        self.assertEqual(section['source_payload']['qualifications'], [])
        self.assertEqual(section['source_payload']['prior_attainment'], attainment)

    def test_plr_permission_error_preserves_available_qualifications_and_stays_partial(self):
        identity, records, _ = self.plr_source_example()
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response(identity), self.response(records),
                ReviewSyncError('HTTP 403', status_code=403)]):
            section = _fetch_plr(MagicMock(), {}, {'userId': 88}, 'PLR')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertEqual(section['source_payload']['qualifications'], records)
        self.assertIn('403', section['capture_error'])

    def test_plr_identity_mismatch_stops_before_other_learner_records_are_fetched(self):
        with patch('aptem.services.review_api_sync.api_get', return_value=self.response(
                {'value': [{'Id': 999, 'UserILRSummary_ULN': 'Other learner'}]})) as get:
            section = _fetch_plr(MagicMock(), {}, {'userId': 88}, 'PLR')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertNotIn('learner_identity', section['source_payload'])
        self.assertEqual(get.call_count, 1)

    def test_plr_rejects_record_identity_mismatch_and_incomplete_attainment(self):
        identity, records, attainment = self.plr_source_example()
        records[0]['userId'] = 999
        del attainment['englishLevel']
        with patch('aptem.services.review_api_sync.api_get', side_effect=[
                self.response(identity), self.response(records), self.response(attainment)]):
            section = _fetch_plr(MagicMock(), {}, {'userId': 88}, 'PLR')
        self.assertEqual(section['capture_state'], 'unavailable')
        self.assertNotIn('qualifications', section['source_payload'])
        self.assertIn('different learner', section['capture_error'])
        self.assertIn('incomplete', section['capture_error'])

    def test_type_guid_read_query_is_fixed_endpoint_empty_body_and_does_not_follow_redirects(self):
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'Data': [], 'Errors': None}
        params = {'userId': 88, 'programId': 222, 'typeGuidToBeIncluded': ''}
        self.assertEqual(_review_type_light_read(session, params), {'Data': [], 'Errors': None})
        request = session.post.call_args
        self.assertTrue(request.args[0].endswith('/MWS.Review/Review/LightRead'))
        self.assertEqual(request.kwargs['params'], params)
        self.assertIsNone(request.kwargs['data'])
        self.assertFalse(request.kwargs['allow_redirects'])

    @patch('aptem.services.review_api_sync._review_type_light_read')
    @patch('aptem.services.review_api_sync.api_get')
    def test_type_guid_recovery_uses_target_identity_and_exact_numeric_type_id(self, get, query):
        guid = '49ef736b-bf5e-4748-bb41-c32a5bec6963'
        get.return_value = self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 222}]})
        query.return_value = {'Data': [{'Id': 352, 'Guid': guid}], 'Total': 1, 'Errors': None}
        recovered, proof = _recover_review_type_guid(MagicMock(), '123', {'userId': 88, 'typeId': 352})
        self.assertEqual(recovered, guid)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['params'], {'userId': 88, 'programId': 222, 'typeGuidToBeIncluded': ''})
        self.assertEqual(proof['response'], query.return_value)
        self.assertEqual(proof['source'], 'target_review_numeric_type_id_mapping')

    @patch('aptem.services.review_api_sync._review_type_light_read')
    @patch('aptem.services.review_api_sync.api_get')
    def test_type_guid_recovery_rejects_wrong_missing_ambiguous_or_invalid_type_records(self, get, query):
        guid = '49ef736b-bf5e-4748-bb41-c32a5bec6963'
        get.return_value = self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 222}]})
        for rows in ([], [{'Id': 999, 'Guid': guid}], [{'Id': '352', 'Guid': guid}],
                     [{'Id': 352}], [{'Id': 352, 'Guid': 'invalid'}],
                     [{'Id': 352, 'Guid': guid}, {'Id': 352, 'Guid': guid}]):
            with self.subTest(rows=rows):
                query.return_value = {'Data': rows, 'Errors': None}
                recovered, proof = _recover_review_type_guid(MagicMock(), '123', {'userId': 88, 'typeId': 352})
                self.assertIsNone(recovered)
                self.assertFalse(proof['verified'])
                self.assertIn('error', proof)

    @patch('aptem.services.review_api_sync._review_type_light_read')
    @patch('aptem.services.review_api_sync.api_get')
    def test_type_guid_recovery_does_not_query_another_learner_or_treat_read_errors_as_success(self, get, query):
        get.return_value = self.response({'value': [{'Id': 123, 'LearnerId': 999, 'ProgramId': 222}]})
        guid, proof = _recover_review_type_guid(MagicMock(), '123', {'userId': 88, 'typeId': 352})
        self.assertIsNone(guid)
        self.assertFalse(proof['verified'])
        query.assert_not_called()
        get.return_value = self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 222}]})
        query.side_effect = ReviewSyncError('Aptem type read query HTTP 403')
        guid, proof = _recover_review_type_guid(MagicMock(), '123', {'userId': 88, 'typeId': 352})
        self.assertIsNone(guid)
        self.assertFalse(proof['verified'])

    @patch('aptem.services.review_api_sync._fetch_section')
    @patch('aptem.services.review_api_sync._review_type_light_read')
    @patch('aptem.services.review_api_sync.api_get')
    def test_recovered_type_guid_never_verifies_template_manifest_membership(self, get, query, fetch):
        guid = '49ef736b-bf5e-4748-bb41-c32a5bec6963'
        get.side_effect = [
            self.response({'id': 123, 'typeId': 352, 'userId': 88}),
            self.response({'id': 122, 'typeId': 352, 'userId': 88}),
            self.response({'id': 122, 'typeGuid': 'sibling-must-not-supply-guid', 'sections': [
                {'name': 'Questions', 'contentRoute': {'action': 'CustomContent'}}]}),
            self.response({'value': [{'Id': 123, 'LearnerId': 88, 'ProgramId': 222}]}),
        ]
        query.return_value = {'Data': [{'Id': 352, 'Guid': guid}], 'Errors': None}
        fetch.return_value = {'section_name': 'Questions', 'fields': [], 'tables': [], 'raw_text': '',
                              'capture_state': 'verified', 'source_payload': {}}
        manifest, sections = fetch_review_content_from_template(MagicMock(), '123', '122')
        self.assertEqual(manifest['typeGuid'], guid)
        self.assertTrue(manifest['type_guid_recovery']['verified'])
        self.assertTrue(manifest['manifest_reconstructed'])
        self.assertFalse(manifest['capture_verified'])
        self.assertFalse(capture_is_verified(manifest, sections))

    @override_settings(APTEM_BASE_URL='https://aptem.example')
    def test_inventory_follows_pagination_and_validates_total(self):
        first, second = MagicMock(), MagicMock()
        first.json.return_value = {'value': [{'Id': 1, 'LearnerId': 88}], '@odata.count': 2,
                                   '@odata.nextLink': '/odata/1.0/Reviews?$skip=1'}
        second.json.return_value = {'value': [{'Id': 2, 'LearnerId': 88}], '@odata.count': 2}
        with patch('aptem.services.review_api_sync.api_get', side_effect=[first, second]) as get:
            self.assertEqual(len(live_inventory(MagicMock())), 2)
            self.assertEqual(get.call_args_list[1].args[1], '/odata/1.0/Reviews?$skip=1')
        second.json.return_value['@odata.count'] = 3
        with patch('aptem.services.review_api_sync.api_get', side_effect=[first, second]):
            with self.assertRaisesRegex(ReviewSyncError, 'changed during'):
                live_inventory(MagicMock())

    @override_settings(APTEM_BASE_URL='https://aptem.example')
    def test_selected_inventory_filters_exact_ids_and_follows_pagination(self):
        first = self.response({'value': [{'Id': 2, 'LearnerId': 88}], '@odata.count': 2,
                               '@odata.nextLink': '/odata/1.0/Reviews?$skiptoken=next'})
        second = self.response({'value': [{'Id': 10, 'LearnerId': 99}], '@odata.count': 2})
        with patch('aptem.services.review_api_sync.api_get', side_effect=[first, second]) as get:
            rows = live_inventory(MagicMock(), review_ids=['10', '2'])
        self.assertEqual([row['Id'] for row in rows], [2, 10])
        self.assertEqual(get.call_args_list[0].kwargs['params'],
                         {'$count': 'true', '$filter': 'Id eq 2 or Id eq 10'})
        self.assertEqual(get.call_args_list[0].kwargs['timeout'], 120)
        self.assertIsNone(get.call_args_list[1].kwargs['params'])

    def test_selected_inventory_rejects_missing_or_out_of_scope_response_ids(self):
        for returned in ([1], [1, 3]):
            response = self.response({'value': [{'Id': rid, 'LearnerId': 88} for rid in returned],
                                      '@odata.count': len(returned)})
            with self.subTest(returned=returned), patch(
                    'aptem.services.review_api_sync.api_get', return_value=response):
                with self.assertRaisesRegex(ReviewSyncError, 'requested review IDs'):
                    live_inventory(MagicMock(), review_ids=['1', '2'])

    def test_selected_inventory_rejects_empty_non_numeric_or_unbounded_requests_before_network(self):
        for selected in ([], ['1 or true'], ['0'], ['-1'], [True], range(1, 202)):
            with self.subTest(selected=selected), patch('aptem.services.review_api_sync.api_get') as get:
                with self.assertRaisesRegex(ReviewSyncError, 'positive numeric'):
                    live_inventory(MagicMock(), review_ids=selected)
                get.assert_not_called()

    @override_settings(APTEM_BASE_URL='https://aptem.example')
    def test_inventory_rejects_off_origin_pagination_without_sending_credentials(self):
        response = MagicMock()
        response.json.return_value = {'value': [{'Id': 1, 'LearnerId': 88}],
                                      '@odata.nextLink': 'https://other.example/odata/1.0/Reviews'}
        with patch('aptem.services.review_api_sync.api_get', return_value=response) as get:
            with self.assertRaisesRegex(ReviewSyncError, 'outside'):
                live_inventory(MagicMock())
        self.assertEqual(get.call_count, 1)

    @patch('aptem.services.review_api_sync.transaction.atomic')
    @patch('aptem.services.review_api_sync.connection')
    def test_metadata_update_does_not_change_last_extracted_time(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            (77, 88, '{"capture_version":4,"sections":[{"kept":true}]}'),
            ('saved', 'success', None),
        ]
        save_live_review({'Id': 123, 'LearnerId': 88}, 88)
        update = next(call for call in cursor.execute.call_args_list
                      if 'SET review_name' in call.args[0])
        self.assertIn('CASE WHEN %s THEN now()', update.args[0])
        self.assertFalse(update.args[1][-2])
        self.assertEqual(json.loads(update.args[1][7])['capture_version'], 4)
        self.assertFalse(any('DELETE FROM' in call.args[0] for call in cursor.execute.call_args_list))

    @patch('aptem.services.review_api_sync.transaction.atomic')
    @patch('aptem.services.review_api_sync.connection')
    def test_partial_upsert_keeps_old_answer_and_cannot_report_success(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (77, 88, '{}')
        cursor.fetchall.return_value = [('Q', '[{"label":"X","value":"saved"}]', '[]', 'saved')]
        manifest = {'id': 123, 'userId': 88, 'sections': [{'name': 'Q'}], 'capture_verified': False}
        sections = [{'section_name': 'Q', 'fields': [], 'tables': [], 'raw_text': '',
                     'source_payload': None, 'capture_state': 'unavailable', 'capture_error': 'HTTP 500'}]
        save_live_review({'Id': 123, 'LearnerId': 88}, 88, manifest, sections)
        update = next(call for call in cursor.execute.call_args_list
                      if 'SET review_name' in call.args[0])
        self.assertEqual(update.args[1][10], 'partial')
        data = json.loads(update.args[1][7])
        self.assertFalse(data['capture_verified'])
        self.assertEqual(data['sections'][0]['raw_text'], 'saved')
        insert = next(call for call in cursor.execute.call_args_list
                      if 'INSERT INTO "Learner".review_sections' in call.args[0])
        self.assertEqual(insert.args[1][-1], 'saved')
