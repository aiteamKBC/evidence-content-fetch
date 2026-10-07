import copy
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from aptem.management.commands.reconcile_preserved_review_sections import (
    Command, reconciliation_plan, reconcile_one,
)


class ReconcilePreservedSectionsTests(SimpleTestCase):
    def setUp(self):
        self.source = {'Id': 123, 'LearnerId': 88, 'Status': 'Completed', 'UpdatedDate': '2026-10-01T12:00:00Z'}
        self.section = {'section_name': 'RAG', 'fields': [{'label': 'RAG Level', 'value': '3'}],
                        'tables': [], 'raw_text': 'RAG Level\n3', 'capture_state': 'verified',
                        'source_action': 'RagContent', 'source_payload': {'level': 3}}
        self.retained = copy.deepcopy(self.section)
        self.retained.update(capture_state='preserved_unverified', source_kind='historical_preserved')
        self.manifest = {'id': 123, 'userId': 88, 'capture_verified': True, 'capture_errors': [],
                         'sections': [{'name': 'RAG', 'contentRoute': {'action': 'RagContent'}}],
                         'supplemental_sections': []}
        sections = [self.section, self.retained]
        self.before = {'review': {'id': 77, 'aptem_review_id': '123', 'learner_id': 9,
                                   'status': 'Completed', 'extraction_status': 'partial',
                                   'last_extracted_at': '2026-10-01T13:00:00+00:00',
                                   'review_data': {'api_meta': self.manifest, 'sections': sections,
                                                  'capture_version': 6, 'content_capture_version': 6,
                                                  'capture_verified': False, 'live_odata': self.source.copy(),
                                                  'aptem_review_id': '123', 'aptem_learner_id': '88',
                                                  'content_source_updated_at': self.source['UpdatedDate']}},
                       'sections': [{k: row[k] for k in ('section_name', 'fields', 'tables', 'raw_text')}
                                    for row in sections]}

    def plan(self):
        return reconciliation_plan(self.before, self.source, 88)

    def test_exact_redundant_capture_is_eligible_without_changing_original(self):
        original = copy.deepcopy(self.before)
        plan, reason = self.plan()
        self.assertIsNone(reason)
        self.assertEqual(plan['sections'], [self.section])
        self.assertEqual(plan['retained_count'], 1)
        self.assertEqual(self.before, original)

    def test_unknown_extra_remains_partial(self):
        self.retained['section_name'] = 'Unrecognised prior answers'
        self.before['sections'][1]['section_name'] = self.retained['section_name']
        plan, reason = self.plan()
        self.assertIsNone(plan)
        self.assertEqual(reason, 'preserved_sections_still_unverified')

    def test_source_change_or_missing_source_is_explicit_skip(self):
        self.source['UpdatedDate'] = '2026-10-02T12:00:00Z'
        self.assertEqual(self.plan()[1], 'source_changed_or_freshness_unproven')
        self.assertEqual(reconciliation_plan(self.before, None, 88)[1], 'source_review_missing')

    def test_canonical_identity_mismatch_is_skipped(self):
        self.assertEqual(reconciliation_plan(self.before, self.source, 99)[1], 'identity_mismatch')

    def test_unavailable_source_or_old_version_cannot_be_reclassified(self):
        self.section['capture_state'] = 'unavailable'
        self.assertEqual(self.plan()[1], 'partial_capture_has_other_failures')
        self.before['review']['review_data']['capture_version'] = 5
        self.assertEqual(self.plan()[1], 'not_eligible_v6_partial_capture')

    def test_false_freshness_from_metadata_update_is_rejected(self):
        self.before['review']['review_data']['content_source_updated_at'] = 'older'
        self.assertEqual(self.plan()[1], 'source_changed_or_freshness_unproven')

    def test_json_table_mismatch_is_rejected(self):
        self.before['sections'][0]['raw_text'] = 'different'
        self.assertEqual(self.plan()[1], 'stored_json_table_mismatch')

    def test_merge_must_not_change_fresh_source_answers(self):
        changed = copy.deepcopy(self.section)
        changed['raw_text'] = 'changed source answer'
        with patch('aptem.management.commands.reconcile_preserved_review_sections.expected_partial_sections',
                   return_value=[changed]):
            self.assertEqual(self.plan()[1], 'reconciliation_would_change_source_capture')

    def test_prior_failed_source_audit_is_not_a_preservation_only_problem(self):
        self.before['review']['review_data']['content_audit'] = {'verified': False, 'reason': 'source failed'}
        self.assertEqual(self.plan()[1], 'non_preservation_audit_failure')

    def test_restoration_audit_failure_can_be_reconciled_when_strictly_proven(self):
        self.before['review']['review_data']['content_audit'] = {
            'verified': False, 'reason': 'Prior sections restored from backup; auxiliary source verification pending'}
        self.assertIsNotNone(self.plan()[0])

    def test_apply_locks_rechecks_backs_up_and_preserves_capture_timestamp(self):
        target = 'aptem.management.commands.reconcile_preserved_review_sections.'
        after = copy.deepcopy(self.before)
        after['review']['review_data'].update(sections=[self.section], capture_verified=True)
        with patch(target + 'connection') as conn, patch(target + 'transaction.atomic'), \
                patch(target + 'read_stored', side_effect=[self.before, after]), \
                patch(target + 'write_backup') as backup, patch(target + 'save_live_review') as save, \
                patch(target + 'verify_readback', return_value='hash') as check:
            cursor = conn.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (77, 88)
            result = reconcile_one('123', self.source, apply=True, backup_directory=Path('unused'))
        self.assertIn('FOR UPDATE OF r', cursor.execute.call_args_list[0].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1],
                         [self.before['review']['last_extracted_at'], 77])
        backup.assert_called_once()
        save.assert_called_once()
        check.assert_called_once()
        self.assertEqual(result['outcome'], 'reconciled')

    def test_dry_run_is_read_only_and_never_saves_or_creates_backup(self):
        target = 'aptem.management.commands.reconcile_preserved_review_sections.'
        with patch(target + 'connection') as conn, patch(target + 'transaction.atomic'), \
                patch(target + 'read_stored', return_value=self.before), \
                patch(target + 'write_backup') as backup, patch(target + 'save_live_review') as save:
            cursor = conn.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (77, 88)
            result = reconcile_one('123', self.source, apply=False, backup_directory=Path('unused'))
        self.assertIn('READ ONLY', cursor.execute.call_args_list[0].args[0])
        self.assertNotIn('FOR UPDATE', cursor.execute.call_args_list[1].args[0])
        backup.assert_not_called()
        save.assert_not_called()
        self.assertEqual(result['outcome'], 'eligible')

    def test_changed_capture_timestamp_raises_inside_transaction(self):
        target = 'aptem.management.commands.reconcile_preserved_review_sections.'
        after = copy.deepcopy(self.before)
        after['review']['last_extracted_at'] = 'new'
        with patch(target + 'connection') as conn, patch(target + 'transaction.atomic'), \
                patch(target + 'read_stored', side_effect=[self.before, after]), \
                patch(target + 'write_backup'), patch(target + 'save_live_review'), \
                patch(target + 'verify_readback', return_value='hash'):
            conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (77, 88)
            with self.assertRaises(CommandError):
                reconcile_one('123', self.source, apply=True, backup_directory=Path('unused'))

    def test_apply_refuses_active_repair_lock_before_reading_source(self):
        target = 'aptem.management.commands.reconcile_preserved_review_sections.'
        with patch(target + 'Path.mkdir'), patch(target + 'Path.open', side_effect=FileExistsError), \
                patch(target + 'login_session') as login:
            with self.assertRaisesRegex(CommandError, 'locked'):
                Command().handle(apply=True, workers=5, review_ids='123', report='unused.json')
        login.assert_not_called()
