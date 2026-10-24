from django.test import SimpleTestCase
from unittest.mock import MagicMock
from pathlib import Path

from aptem.services.review_signature_completion import CompletionClient, classify_pdf, signature_blocks
from aptem.services.review_signatures import SignatureError


class SignatureCompletionTests(SimpleTestCase):
    text = '''Advisor
Name: Coach Person
Position: Coach
Review
Page 7
Signature:
Date: 18/03/2026
Employer
Name:
Position:
Signature:
Date:
Participant
Name: Learner Person
Signature:
Date: 18/03/2026
Review
Page 8
'''

    def result(self,text=None,groups=None):
        return classify_pdf({'roles':{},'state':'unknown'},
            {'Id':14240,'LearnerId':2413,'OwnerId':1162,'OwnerName':'Current coach','Status':'AwaitingSignature'},
            groups if groups is not None else [{'AdvisorSignerId':1162,'ParticipantSignerId':2413,
                'EmployerSignerId':None,'ReferrerSignerId':None,'IsFullySigned':False}],
            {'Id':8837},self.text if text is None else text,'hash','2026-10-23T18:00:00+00:00')

    def test_split_pages_and_blank_fields_identify_missing_employer(self):
        result=self.result()
        self.assertTrue(result['verified'])
        self.assertEqual(result['missing_roles'],['employer'])
        self.assertEqual(result['roles']['coach']['signed_by_name'],'Coach Person')
        self.assertEqual(result['roles']['coach']['signed_at'],'2026-03-18')
        self.assertEqual(result['roles']['coach']['follow_up_contact']['name'],'Current coach')

    def test_old_required_mask_zero_does_not_override_signed_pdf(self):
        group={'RequiredSigner':0,'AdvisorSignerId':1162,'ParticipantSignerId':2413,
               'EmployerSignerId':None,'ReferrerSignerId':None,'IsFullySigned':False,'ShouldSign':True}
        self.assertEqual(self.result(groups=[group])['state'],'missing')

    def test_disagreeing_signers_remain_unknown(self):
        group={'AdvisorSignerId':999,'ParticipantSignerId':2413,'EmployerSignerId':None,'ReferrerSignerId':None}
        other={**group,'AdvisorSignerId':1162}
        self.assertFalse(self.result(groups=[group,other])['verified'])

    def test_absent_signature_text_is_not_enough_to_call_not_required(self):
        self.assertEqual(self.result(text='Monthly Coaching Meeting')['state'],'unknown')
        result=self.result(text='Monthly Coaching Meeting',groups=[{'ShouldSign':False,'RequiredSigner':0}])
        self.assertEqual(result['state'],'not_required')
        self.assertTrue(result['verified'])

    def test_absent_group_and_conflicting_completion_fail_closed(self):
        self.assertEqual(self.result(groups=[])['state'],'unknown')
        self.assertEqual(self.result(groups=[{'AdvisorSignerId':1162,'ParticipantSignerId':2413,
                        'IsFullySigned':True}])['state'],'unknown')

    def test_signature_dates_require_names_and_source_signer_id(self):
        text=self.text.replace('Name: Coach Person','Name:')
        self.assertFalse(self.result(text=text)['verified'])

    def test_duplicate_signature_blocks_are_rejected(self):
        with self.assertRaises(SignatureError):
            signature_blocks(self.text+self.text)

    def test_empty_document_inventory_is_verified_without_inventing_missing_roles(self):
        client=CompletionClient(MagicMock())
        client.documents=MagicMock(return_value=[])
        client.download=MagicMock()
        row={'aptem_review_id':'1','aptem_learner_id':'2','learner_name':'Learner',
             'before':{'state':'unknown','verified':False,'roles':{}}}
        result=client.resolve(row,{'Id':1,'LearnerId':2,'Status':'Scheduled'},[],Path('.'))['after']
        self.assertEqual(result['state'],'document_not_generated')
        self.assertTrue(result['verified'])
        self.assertEqual(result['missing_roles'],[])
        self.assertIsNone(result['roles']['learner']['required'])
        client.download.assert_not_called()

    def test_source_learner_mismatch_is_rejected_before_document_reads(self):
        client=CompletionClient(MagicMock())
        client.documents=MagicMock()
        row={'aptem_review_id':'1','aptem_learner_id':'2'}
        with self.assertRaises(SignatureError):
            client.resolve(row,{'Id':1,'LearnerId':999},[],Path('.'))
        client.documents.assert_not_called()

    def test_deleted_template_records_document_fact_without_guessing_requirements(self):
        result=classify_pdf({'roles':{}},{'Status':'Completed'},
            [{'ShouldSign':False,'RequiredSigner':None}],{'Id':1,'IsUploaded':False},
            'Monthly meeting content without any signature controls. '*10,'hash','2026-10-23')
        self.assertEqual(result['state'],'no_signature_fields')
        self.assertIsNone(result['roles']['learner']['required'])
        self.assertEqual(result['missing_roles'],[])
        self.assertTrue(result['verified'])
