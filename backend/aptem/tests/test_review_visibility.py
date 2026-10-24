from unittest.mock import patch, MagicMock
import tempfile
from pathlib import Path
from django.test import SimpleTestCase

from aptem.services.review_visibility import plan_membership, reconcile_visibility, restore_visibility, reconcile_batch
from aptem.services.review_signatures import write_verified, load_verified
from aptem.services.review_api_sync import _payload


class ReviewVisibilityTests(SimpleTestCase):
    def snapshot(self, ids=None):
        return {'learner_id': '100', 'review_ids': ['12'] if ids is None else ids,
                'checked_at': '2026-01-02T00:00:00+00:00'}

    def test_exact_membership_retains_old_identity_and_can_reappear(self):
        first = plan_membership([(1, '12', None), (2, '99', None)], self.snapshot(), '100')
        self.assertEqual([r['id'] for r in first], [1, 2])
        self.assertEqual([r['after']['visible'] for r in first], [True, False])
        again = plan_membership([(r['id'], r['aptem_review_id'], r['after']) for r in first],
                                self.snapshot(['99']), '100')
        self.assertEqual([r['after']['visible'] for r in again], [False, True])

    def test_verified_empty_inventory_marks_all_historical(self):
        rows = plan_membership([(1, '12', None)], self.snapshot([]), '100')
        self.assertFalse(rows[0]['after']['visible'])

    def test_missing_visible_row_prevents_hiding_existing_rows(self):
        with self.assertRaisesRegex(ValueError, 'missing from storage'):
            plan_membership([(2, '99', None)], self.snapshot(), '100')

    def test_wrong_identity_duplicate_and_invalid_ids_rejected(self):
        for snapshot in [dict(self.snapshot(), learner_id='101'), self.snapshot(['12', '12']),
                         self.snapshot(['bad']), dict(self.snapshot(), checked_at='2026-01-02')]:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                plan_membership([(1, '12', None)], snapshot, '100')

    def test_old_inventory_cannot_override_newer_membership(self):
        prior = {'checked_at': '2026-01-03T00:00:00+00:00'}
        with self.assertRaisesRegex(ValueError, 'newer'):
            plan_membership([(1, '12', prior)], self.snapshot(), '100')

    def test_content_refresh_retains_visibility_and_signatures(self):
        old = {'aptem_visibility': {'visible': False}, 'signature_status': {'state': 'missing'}}
        data = _payload({'Id': 12, 'LearnerId': 100}, old_data=old)
        self.assertEqual(data['aptem_visibility'], old['aptem_visibility'])
        self.assertEqual(data['signature_status'], old['signature_status'])

    def test_failed_backup_prevents_database_update(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ('100',)
        cursor.fetchall.return_value = [(1, '12', None)]
        with patch('aptem.services.review_visibility.load_verified', return_value=self.snapshot()), \
                patch('aptem.services.review_visibility.transaction.atomic'), \
                patch('aptem.services.review_visibility.connection') as connection, \
                patch('aptem.services.review_visibility.Path.exists', return_value=False), \
                patch('aptem.services.review_visibility.write_verified', side_effect=OSError('disk full')):
            connection.cursor.return_value.__enter__.return_value = cursor
            with self.assertRaises(OSError):
                reconcile_visibility('100', 50, 'visible.json', 'recovery.json', apply=True)
        self.assertFalse(any(call.args[0].lstrip().startswith('UPDATE')
                             for call in cursor.execute.call_args_list))

    def test_recovery_refuses_changed_metadata(self):
        recovery = {'canonical_id': 50, 'rows': [{'id': 1, 'before': None, 'after': {'visible': True}}]}
        cursor = MagicMock(rowcount=0)
        with patch('aptem.services.review_visibility.load_verified', return_value=recovery), \
                patch('aptem.services.review_visibility.transaction.atomic'), \
                patch('aptem.services.review_visibility.connection') as connection:
            connection.cursor.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(ValueError, 'changed since backup'):
                restore_visibility('recovery.json')

    def test_batch_preserves_each_learners_exact_ids_and_checks_readback(self):
        snapshots = [self.snapshot(), dict(self.snapshot(['99']), learner_id='101')]
        planned = [plan_membership([(1, '12', None), (2, '88', None)], snapshots[0], '100'),
                   plan_membership([(3, '99', None)], snapshots[1], '101')]
        readback = [(r['id'], r['after']) for group in planned for r in group]
        for bad in (False, True):
            with self.subTest(bad_readback=bad), tempfile.TemporaryDirectory() as directory:
                specs = []
                for i, snapshot in enumerate(snapshots):
                    path = Path(directory)/f'{i}.json'
                    write_verified(path, snapshot)
                    specs.append((snapshot['learner_id'], 50+i*10, path, Path(directory)/f'{i}-before.json'))
                cursor = MagicMock()
                cursor.fetchall.side_effect = [[(50, '100'), (60, '101')],
                    [(50, 1, '12', None), (50, 2, '88', None), (60, 3, '99', None)],
                    readback[:-1] if bad else readback]
                with patch('aptem.services.review_visibility.transaction.atomic'), \
                        patch('aptem.services.review_visibility.connection') as connection:
                    connection.cursor.return_value.__enter__.return_value = cursor
                    if bad:
                        with self.assertRaisesRegex(ValueError, 'readback'):
                            reconcile_batch(specs, apply=True)
                    else:
                        result = reconcile_batch(specs, apply=True)
                        self.assertEqual(result['100']['visible'], 1)
                        self.assertEqual(result['100']['historical'], 1)
                        self.assertEqual(result['101']['visible'], 1)
                self.assertEqual(len(load_verified(specs[0][3])['rows']), 2)
