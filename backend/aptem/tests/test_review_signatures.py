import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aptem.services.review_api_sync import _payload
from aptem.services.review_signatures import (
    SignatureClient, SignatureError, apply_rows, classify, load_verified, write_verified,
)


def evidence():
    return {'aptem_learner_id': '6177', 'checked_at': '2026-10-23T17:00:00+00:00',
        'groups': [{'ItemId': 9165, 'ItemType': 9, 'DocCount': 4, 'ProgramId': 6593,
            'IsFullySigned': False, 'ParticipantSignerId': 6177, 'AdvisorSignerId': None,
            'EmployerSignerId': 6176, 'ReferrerSignerId': None}],
        'signatures': [{'ItemId': 9165, 'ItemType': 9, 'EntityId': 6177, 'DocumentId': 19423,
            'EntityName': 'Learner', 'ParticipantId': 6177, 'ParticipantSignature': 1,
            'ParticipantSignerName': 'Learner', 'ParticipantSignerDate': '2026-10-23',
            'AdvisorSignature': 0, 'AdvisorSignerId': None,
            'EmployerSignature': 1, 'EmployerSignerId': 6176, 'EmployerSignerName': 'Manager',
            'ReferrerSignature': 2, 'ReferrerSignerId': None,
            'OwnerId': 5277, 'OwnerName': 'Current coach', 'ManagerId': 6176, 'ManagerName': 'Manager'}]}


class ReviewSignaturesTests(SimpleTestCase):
    def test_real_pr_shape_reports_only_missing_coach_and_separates_contact(self):
        result = classify('9165', '6177', evidence())
        self.assertEqual(result['state'], 'missing')
        self.assertEqual(result['missing_roles'], ['coach'])
        self.assertEqual(result['signed_roles'], ['learner', 'employer'])
        self.assertEqual(result['roles']['coach']['follow_up_contact']['name'], 'Current coach')
        self.assertIsNone(result['roles']['coach']['signed_by_id'])
        self.assertEqual(result['roles']['referrer']['status'], 'not_required')

    def test_mcm_all_na_is_not_missing_or_signed(self):
        value = evidence()
        for prefix in ('Participant', 'Advisor', 'Employer', 'Referrer'):
            value['signatures'][0][prefix+'Signature'] = 2
            value['signatures'][0]['ParticipantId' if prefix == 'Participant' else prefix+'SignerId'] = None
            value['groups'][0][prefix+'SignerId'] = None
        value['groups'][0]['IsFullySigned'] = True
        result = classify('9165', '6177', value)
        self.assertEqual(result['state'], 'not_required')
        self.assertEqual(result['missing_roles'], [])

    def test_no_document_is_not_missing(self):
        value = evidence()
        value['signatures'] = []
        value['groups'][0]['DocCount'] = 0
        result = classify('9165', '6177', value)
        self.assertEqual(result['state'], 'document_not_generated')
        self.assertIsNone(result['roles']['coach']['required'])

    def test_signed_by_is_actual_signer_not_current_owner(self):
        value = evidence()
        value['signatures'][0].update(AdvisorSignature=1, AdvisorSignerId=700, AdvisorSignerName='Previous coach')
        value['groups'][0].update(AdvisorSignerId=700, IsFullySigned=True)
        result = classify('9165', '6177', value)
        self.assertEqual(result['state'], 'signed')
        self.assertEqual(result['roles']['coach']['signed_by_id'], 700)
        self.assertEqual(result['roles']['coach']['follow_up_contact']['aptem_id'], 5277)

    def test_unknown_enum_missing_doc_and_conflicts_never_generate_tickets(self):
        for mutation in ('enum', 'missing_doc', 'conflict', 'duplicate', 'full', 'absent'):
            with self.subTest(mutation=mutation):
                value = evidence()
                if mutation == 'enum': value['signatures'][0]['AdvisorSignature'] = 9
                if mutation == 'missing_doc': value['signatures'] = []
                if mutation == 'conflict': value['groups'][0]['EmployerSignerId'] = 123
                if mutation == 'duplicate': value['signatures'] *= 2
                if mutation == 'full': value['groups'][0]['IsFullySigned'] = True
                if mutation == 'absent': value['groups'] = []
                result = classify('9165', '6177', value)
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result['missing_roles'], [])
                self.assertFalse(result['verified'])

    def test_wrong_learner_is_rejected(self):
        with self.assertRaises(SignatureError): classify('9165', '999', evidence())
        value = evidence()
        value['signatures'][0]['EntityId'] = 999
        with self.assertRaises(SignatureError): classify('9165', '6177', value)

    def test_grid_paginates_to_total_even_with_short_pages(self):
        client = SignatureClient(MagicMock())
        client.page = MagicMock(side_effect=[{'Data': [{'Id': 1}], 'Total': 2}, {'Data': [{'Id': 2}], 'Total': 2}])
        self.assertEqual(len(client.rows('episodes', 'Id')), 2)
        self.assertEqual(client.page.call_args.args[1]['skip'], 1)

    def test_pagination_repetition_or_changing_total_is_rejected(self):
        for second in ({'Data': [{'Id': 1}], 'Total': 2}, {'Data': [], 'Total': 2}, {'Data': [{'Id': 2}], 'Total': 3}):
            client = SignatureClient(MagicMock())
            client.page = MagicMock(side_effect=[{'Data': [{'Id': 1}], 'Total': 2}, second])
            with self.assertRaises(SignatureError): client.rows('episodes', 'Id')

    def test_backup_checksum_detects_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'recovery.json'
            write_verified(path, {'before_present': True, 'before': None})
            self.assertEqual(load_verified(path)['before'], None)
            path.write_text('{}')
            with self.assertRaises(SignatureError): load_verified(path)

    def test_regular_import_preserves_signature_enrichment(self):
        signature = classify('9165', '6177', evidence())
        previous = {'signature_status': deepcopy(signature), 'sections': [{'raw_text': 'keep'}]}
        result = _payload({'Id': 9165, 'LearnerId': 6177}, old_data=previous)
        self.assertEqual(result['signature_status'], signature)
        self.assertEqual(result['sections'], previous['sections'])

    @patch('aptem.services.review_signatures.transaction.atomic')
    @patch('aptem.services.review_signatures.connection')
    def test_apply_rejects_concurrent_changes_before_any_update(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, 3, '9165', '6177', True, {'state': 'newer'}, 'object')]
        row = dict(id=1, learner_id=3, aptem_review_id='9165', aptem_learner_id='6177',
                   before_present=False, before=None, before_null=False, after={'state': 'missing'})
        with self.assertRaisesRegex(SignatureError, 'Concurrent'):
            apply_rows([row])
        self.assertFalse(any('UPDATE "Learner"' in call.args[0] for call in cursor.execute.call_args_list))

    @patch('aptem.services.review_signatures.transaction.atomic')
    @patch('aptem.services.review_signatures.connection')
    def test_restore_preserves_explicit_json_null_and_is_idempotent(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        row = dict(id=1, learner_id=3, aptem_review_id='9165', aptem_learner_id='6177',
                   before_present=True, before=None, before_null=False, after={'state': 'missing'})
        cursor.fetchall.side_effect = [[(1, 3, '9165', '6177', True, row['after'], 'object')], [(1, True, None)]]
        self.assertEqual(apply_rows([row], restore=True), 1)
        update = next(call for call in cursor.execute.call_args_list if 'UPDATE "Learner"' in call.args[0])
        value = json.loads(update.args[1][3])[0]
        self.assertTrue(value['present'])
        self.assertIsNone(value['value'])
        cursor.fetchall.side_effect = [[(1, 3, '9165', '6177', True, None, 'object')]]
        self.assertEqual(apply_rows([row], restore=True), 0)

    @patch('aptem.services.review_signatures.transaction.atomic')
    @patch('aptem.services.review_signatures.connection')
    def test_apply_rejects_wrong_canonical_learner(self, connection, atomic):
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, 999, '9165', '6177', False, None, 'object')]
        row = dict(id=1, learner_id=3, aptem_review_id='9165', aptem_learner_id='6177',
                   before_present=False, before=None, before_null=False, after={'state': 'missing'})
        with self.assertRaisesRegex(SignatureError, 'identity'):
            apply_rows([row])
