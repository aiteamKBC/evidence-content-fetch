import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from aptem.management.commands.audit_active_learner_reviews import Command, persist_review
from aptem.services.review_audit import classify, checkpoint, active_snapshot, shard_records
from aptem.services.review_probe import ProbeError


def record():
    return {'learner_id': 1, 'mapping_status': 'matched', 'status': 'pending',
            'attempts': 0, 'errors': [], 'saved_review_ids': [],
            'saved_completed_ids': [], 'in_progress_metadata_only_ids': [],
            'planned_inventory_complete': False, 'finished_inventory_complete': False,
            'expected_finished': None}


class AuditTests(SimpleTestCase):
    def test_four_workers_cover_each_learner_exactly_once(self):
        records = {str(i): record() for i in range(1, 350)}
        shards = [shard_records(records, 4, i) for i in range(4)]
        self.assertEqual(sum(len(shard) for shard in shards), len(records))
        self.assertEqual(set().union(*(set(shard) for shard in shards)), set(records))
        for i, shard in enumerate(shards):
            for other in shards[i + 1:]:
                self.assertFalse(set(shard) & set(other))

    def test_five_workers_cover_each_learner_exactly_once(self):
        records = {str(i): record() for i in range(1, 700)}
        shards = [shard_records(records, 5, i) for i in range(5)]
        self.assertEqual(sum(len(shard) for shard in shards), len(records))
        self.assertEqual(set().union(*(set(shard) for shard in shards)), set(records))
        for i, shard in enumerate(shards):
            for other in shards[i + 1:]:
                self.assertFalse(set(shard) & set(other))

    def test_browser_callback_saves_on_separate_thread(self):
        caller = threading.get_ident()
        with patch('aptem.management.commands.audit_active_learner_reviews.save_review',
                   side_effect=lambda payload: threading.get_ident()), \
             patch('aptem.management.commands.audit_active_learner_reviews.connection') as db:
            self.assertNotEqual(persist_review({}), caller)
            db.close.assert_called_once()

    def test_database_failure_propagates_to_checkpoint_owner(self):
        with patch('aptem.management.commands.audit_active_learner_reviews.save_review',
                   side_effect=ValueError('write failed')), \
             patch('aptem.management.commands.audit_active_learner_reviews.connection') as db:
            with self.assertRaisesRegex(ValueError, 'write failed'):
                persist_review({})
            db.close.assert_called_once()

    def test_empty_requires_both_inventories(self):
        data = record()
        self.assertEqual(classify(data), 'partial')
        data.update(planned_inventory_complete=True, finished_inventory_complete=True, expected_finished=0)
        self.assertEqual(classify(data), 'no_reviews')

    def test_missing_completed_review_and_in_progress_are_partial(self):
        data = record()
        data.update(planned_inventory_complete=True, finished_inventory_complete=True,
                    expected_finished=2, saved_review_ids=['a'], saved_completed_ids=['a'])
        self.assertEqual(classify(data), 'partial')
        data['expected_finished'] = 1
        self.assertEqual(classify(data), 'complete')
        data['in_progress_metadata_only_ids'] = ['b']
        self.assertEqual(classify(data), 'partial')

    def test_failure_preserves_partial_progress(self):
        data = record()
        data['errors'] = [{'category': 'finished', 'type': 'TimeoutError'}]
        self.assertEqual(classify(data), 'failed')
        data['saved_review_ids'] = ['a']
        self.assertEqual(classify(data), 'partial')

    def test_duplicate_source_email_is_not_automatically_mapped(self):
        with patch('aptem.services.review_audit.connection') as db:
            cursor = db.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [
                [(1, 'A', 'same@example.org', ''), (2, 'B', 'SAME@example.org', '')],
                [(9, 'A', 'same@example.org', '')], [],
            ]
            data = active_snapshot()
        self.assertTrue(all(r['mapping_status'] == 'unresolved' for r in data.values()))

    def test_gateway_block_leaves_unvisited_learners_pending(self):
        records = {'1': record(), '2': record()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.json'
            with patch('aptem.management.commands.audit_active_learner_reviews.active_snapshot', return_value=records), \
                 patch('aptem.management.commands.audit_active_learner_reviews.aptem_page',
                       side_effect=ProbeError('access_denied: HTTP 403')), \
                 patch('aptem.management.commands.audit_active_learner_reviews.save_review') as save:
                with self.assertRaises(CommandError):
                    Command().run_audit(path, {'resume': False, 'fetch': True})
                save.assert_not_called()
            report = json.loads(path.read_text())
            self.assertEqual(report['run_status'], 'blocked')
            self.assertEqual(report['summary'], {'access_blocked': 1, 'pending': 1})
            self.assertFalse(path.with_suffix('.json.tmp').exists())
