import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aptem.services import visible_review_cycle as cycle


class VisibleReviewCycleTests(SimpleTestCase):
    def test_lock_rejects_concurrent_owner_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.lock'
            with self.assertRaisesRegex(ValueError, 'test'):
                with cycle.local_lock(path):
                    with self.assertRaises(cycle.AlreadyRunning):
                        with cycle.local_lock(path):
                            self.fail('Concurrent owner acquired lock')
                    raise ValueError('test')
            with cycle.local_lock(path):
                pass

    def test_windows_temporary_access_denial_retries_verified_write(self):
        with patch.object(cycle, 'write_verified', side_effect=[PermissionError(), None]) as write, \
                patch.object(cycle.time, 'sleep'):
            cycle.save(Path('report.json'), {'ok': True})
        self.assertEqual(write.call_count, 2)

    def test_only_browser_visible_ids_are_imported_and_backed_up_first(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cycle, 'aptem_page'), \
                patch.object(cycle, 'classic_visible_ids', return_value=(['12', '13'], 0, 2)), \
                patch.object(cycle, 'backup', return_value={'reviews': []}) as backup, \
                patch.object(cycle, 'sync_selected', return_value={}) as sync, \
                patch.object(cycle, 'verify_saved_visible') as verify, \
                patch.object(cycle, 'reconcile_visibility', return_value={}) as visibility, \
                patch.object(cycle, 'refresh_signatures', return_value={'errors': {}}) as signatures:
            order = MagicMock()
            order.attach_mock(backup, 'backup')
            order.attach_mock(sync, 'sync')
            result = cycle.sync_learner('100', 50, Path(directory))
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(order.mock_calls[0][0], 'backup')
            sync.assert_called_once_with('100', 50, ['12', '13'], classic_only=True, refresh_visible_panels=True)
            verify.assert_called_once_with(50, ['12', '13'])
            self.assertEqual(signatures.call_args.args[1], ['12', '13'])
            visibility.assert_called_once()

    def test_bad_inventory_prevents_all_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cycle, 'aptem_page'), \
                patch.object(cycle, 'classic_visible_ids', return_value=(['12', '12'], 0, 2)), \
                patch.object(cycle, 'backup') as backup, patch.object(cycle, 'sync_selected') as sync:
            with self.assertRaisesRegex(ValueError, 'duplicated'):
                cycle.sync_learner('100', 50, Path(directory))
            backup.assert_not_called()
            sync.assert_not_called()

    def test_visible_review_with_content_error_stays_in_membership(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cycle, 'aptem_page'), \
                patch.object(cycle, 'classic_visible_ids', return_value=(['12'], 0, 1)), \
                patch.object(cycle, 'backup', return_value={'reviews': []}), \
                patch.object(cycle, 'sync_selected', return_value={}), \
                patch.object(cycle, 'verify_saved_visible', side_effect=ValueError('Aptem content unavailable')), \
                patch.object(cycle, 'reconcile_visibility', return_value={'visible': 1}) as visibility, \
                patch.object(cycle, 'refresh_signatures', return_value={'errors': {}}):
            result = cycle.sync_learner('100', 50, Path(directory))
        visibility.assert_called_once()
        self.assertEqual(result['visibility']['visible'], 1)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('content_verification', result['errors'])

    def test_failed_signature_verification_keeps_old_metadata_and_excludes_hidden(self):
        rows = [dict(aptem_review_id='12', before={'verified': True}), dict(aptem_review_id='99')]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cycle, 'inventory', return_value=rows), \
                patch.object(cycle, 'CompletionClient') as client, \
                patch.object(cycle, 'classify', return_value={'verified': False}), \
                patch.object(cycle, 'api_get') as api, \
                patch.object(cycle, 'apply_rows') as apply:
            api.return_value.json.return_value = {'value': []}
            client.return_value.learner.return_value = {'groups': []}
            client.return_value.resolve.side_effect = ValueError('source unavailable')
            result = cycle.refresh_signatures('100', ['12'], Path(directory))
            self.assertEqual(set(result['errors']), {'12'})
            apply.assert_not_called()
            self.assertEqual(rows[0]['before'], {'verified': True})
