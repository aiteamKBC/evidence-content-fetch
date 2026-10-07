from copy import deepcopy
from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from aptem.management.commands.restore_review_capture_backups import Command, omitted_sections


class CaptureBackupRestoreTests(SimpleTestCase):
    def section(self, name, text):
        return {'section_name': name, 'fields': [], 'tables': [], 'raw_text': text}

    def test_restore_only_omitted_content_without_reverting_changed_answers(self):
        old = [self.section('Question', 'old'), self.section('Meeting Summary', 'notes'),
               self.section('Empty', '')]
        before = {'review': {'review_data': {'sections': deepcopy(old)}}, 'sections': old}
        current = {'sections': [self.section('Question', 'corrected')]}
        result = omitted_sections(before, current)
        self.assertEqual([r['section_name'] for r in result], ['Meeting Summary'])
        self.assertEqual(result[0]['raw_text'], 'notes')
        self.assertEqual(result[0]['capture_state'], 'preserved_unverified')
        self.assertEqual(current['sections'][0]['raw_text'], 'corrected')

    def test_repeated_restore_is_idempotent(self):
        old = [self.section('Meeting Summary', 'notes')]
        before = {'review': {'review_data': {}}, 'sections': old}
        current = {'sections': deepcopy(old)}
        self.assertEqual(omitted_sections(before, current), [])

    def test_same_named_sections_keep_multiplicity(self):
        old = [self.section('Notes', 'first'), self.section('Notes', 'second')]
        before = {'review': {'review_data': {}}, 'sections': old}
        current = {'sections': [deepcopy(old[0])]}
        additions = omitted_sections(before, current)
        self.assertEqual([s['raw_text'] for s in additions], ['second'])
        current['sections'].extend(additions)
        self.assertEqual(omitted_sections(before, current), [])

    @patch('aptem.management.commands.restore_review_capture_backups.transaction')
    @patch('aptem.management.commands.restore_review_capture_backups.connection')
    @patch('aptem.management.commands.restore_review_capture_backups.read_stored')
    def test_apply_rechecks_after_lock_and_skips_already_restored_content(self, read, database, transaction):
        before = {'review': {'id': 1, 'learner_id': 2, 'aptem_review_id': '42', 'review_data': {}},
                  'sections': [self.section('Meeting Summary', 'notes')]}
        read.return_value = deepcopy(before)
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        database.cursor.return_value.__enter__.return_value = cursor
        self.assertEqual(Command().restore_one(before, 'test-backup'), 0)
        cursor.execute.assert_called_once()
        self.assertIn('FOR UPDATE', cursor.execute.call_args.args[0])

    @patch('aptem.management.commands.restore_review_capture_backups.transaction')
    @patch('aptem.management.commands.restore_review_capture_backups.connection')
    @patch('aptem.management.commands.restore_review_capture_backups.read_stored')
    def test_restore_refuses_changed_canonical_learner(self, read, database, transaction):
        before = {'review': {'id': 1, 'learner_id': 2, 'aptem_review_id': '42', 'review_data': {}},
                  'sections': [self.section('Meeting Summary', 'notes')]}
        current = deepcopy(before)
        current['review']['learner_id'] = 99
        read.return_value = current
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        database.cursor.return_value.__enter__.return_value = cursor
        with self.assertRaisesRegex(CommandError, 'identity differs'):
            Command().restore_one(before, 'test-backup')
        self.assertEqual(cursor.execute.call_count, 1)
