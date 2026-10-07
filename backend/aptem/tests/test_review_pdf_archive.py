import hashlib
import io
import math
import threading
from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceExistsError
from django.test import SimpleTestCase, override_settings
from pypdf import PdfWriter
from rest_framework.test import APIRequestFactory, force_authenticate

from aptem.review_document_views import learner_documents, download_document
from aptem.management.commands.sync_review_pdfs import Command
from aptem.services.review_pdf_lease import ReviewArchiveLease, LeaseLost
from aptem.services.review_pdf_inventory import map_learner, match_review
from aptem.services.review_pdf_transfer import (
    download_pdf, inspect_pdf, ReviewBlobStore, TransferError, azure_error,
)


def document():
    return {'id':1,'source_evidence_id':42,'aptem_learner_id':777,'learner_id':12,
        'source_url':'https://kentbusinesscollege.aptem.co.uk/MWS.LearningProgrammes/LearningItemEvidence/Document/42'}


class IdentityTests(SimpleTestCase):
    def test_aptem_id_is_not_internal_learner_id(self):
        learners = [{'id':777,'aptem_id':99,'email':'wrong@x'},
                    {'id':12,'aptem_id':777,'email':'right@x'}]
        self.assertEqual(map_learner(777,[],learners),(12,'aptem_id'))

    def test_conflicting_email_mapping_is_not_guessed(self):
        users = [{'id':777,'email':'same@x'}]
        learners = [{'id':12,'aptem_id':999,'email':'same@x'}]
        self.assertEqual(map_learner(777,users,learners),(None,'conflicting_identity'))

    def test_duplicate_email_blocks_fallback(self):
        users = [{'id':777,'email':'same@x'},{'id':999,'email':'SAME@x'}]
        self.assertEqual(map_learner(777,users,[{'id':12,'aptem_id':None,'email':'same@x'}]),(None,'ambiguous_email'))

    def test_review_link_requires_unique_learner_type_and_pdf_dates(self):
        meta = {'title':'Progress Review','completed_date':'2026-04-29','planned_date':'2026-04-28'}
        review = {'id':30,'learner_id':12,'status':'Completed', 'review_name':'PR 2',
            'review_type':'Progress Review', 'review_data':{'aptem_learner_id':'777',
                'source_metadata':{'Completed Date':'29 Apr 2026','Planned / Scheduled Date':'28 Apr 2026 at 10:00'}}}
        self.assertEqual(match_review(document(),meta,[review]),(30,'matched_pdf_type_dates'))
        self.assertEqual(match_review(document(),meta,[review,dict(review,id=31)]),(None,'ambiguous_review'))
        self.assertEqual(match_review(document(),dict(meta,planned_date='2026-04-27'),[review]),(None,'review_not_found'))
        self.assertEqual(match_review(document(),meta,[dict(review,learner_id=777)]),(None,'review_not_found'))
        review['review_data']['aptem_learner_id'] = '999'
        self.assertEqual(match_review(document(),meta,[review]),(None,'review_not_found'))


@override_settings(REVIEW_PDF_MAX_BYTES=100000)
class TransferTests(SimpleTestCase):
    def test_download_preserves_original_bytes(self):
        body = b'%PDF-1.7\noriginal signed bytes\n%%EOF'
        response = MagicMock(status_code=200,is_redirect=False,headers={'Content-Type':'application/pdf'})
        response.__enter__.return_value = response
        response.iter_content.return_value = [body[:10],body[10:]]
        session = MagicMock()
        session.get.return_value = response
        auth = MagicMock(cookie='PRIVATE')
        stream,size,digest = download_pdf(document(),auth,session)
        with stream:
            self.assertEqual(stream.read(),body)
        self.assertEqual(size,len(body))
        self.assertEqual(digest,hashlib.sha256(body).hexdigest())
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        auth.refresh.assert_not_called()

    def test_mismatched_evidence_id_never_downloaded(self):
        doc = document()
        doc['source_evidence_id'] = 43
        session = MagicMock()
        with self.assertRaisesRegex(TransferError,'identity mismatch'):
            download_pdf(doc,MagicMock(),session)
        session.get.assert_not_called()

    def test_access_denied_redirect_refreshes_session_once(self):
        denied = MagicMock(is_redirect=True,headers={'Location':'/Users/Account/AccessDenied?ReturnUrl=doc'})
        denied.__enter__.return_value = denied
        ok = MagicMock(is_redirect=False,status_code=200,headers={'Content-Type':'application/pdf'})
        ok.__enter__.return_value = ok
        ok.iter_content.return_value = [b'%PDF-1.7\n%%EOF']
        session = MagicMock()
        session.get.side_effect = [denied,ok]
        auth = MagicMock(cookie='old')
        stream,_,_ = download_pdf(document(),auth,session)
        stream.close()
        auth.refresh.assert_called_once_with('old')
        self.assertEqual(session.get.call_count,2)

    def test_external_redirect_not_followed(self):
        response = MagicMock(is_redirect=True,headers={'Location':'https://elsewhere.example/private'})
        response.__enter__.return_value = response
        session = MagicMock()
        session.get.return_value = response
        with self.assertRaisesRegex(TransferError,'no credentials forwarded'):
            download_pdf(document(),MagicMock(cookie='secret'),session)
        self.assertEqual(session.get.call_count,1)

    def test_oversized_download_is_rejected(self):
        response = MagicMock(status_code=200,is_redirect=False,headers={})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'%PDF-'+b'x'*100000]
        session = MagicMock()
        session.get.return_value = response
        with self.assertRaisesRegex(TransferError,'size limit'):
            download_pdf(document(),MagicMock(),session)

    def test_pdf_parser_preserves_file_and_does_not_claim_signatures(self):
        stream = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72,height=72)
        writer.write(stream)
        original = stream.getvalue()
        meta = inspect_pdf(stream)
        self.assertEqual(meta['page_count'],1)
        self.assertEqual(meta['signature_verification'],'not_verified')
        self.assertEqual(stream.read(),original)

    def test_broken_pdf_rejected(self):
        with self.assertRaises(TransferError):
            inspect_pdf(io.BytesIO(b'not a pdf'))


@override_settings(AZURE_REVIEWS_CONTAINER_URL='https://example.blob.core.windows.net/reviews?sig=SECRET')
class BlobTests(SimpleTestCase):
    def test_reupload_verifies_existing_bytes_without_overwriting(self):
        body = b'%PDF-1.7\noriginal\n%%EOF'
        digest = hashlib.sha256(body).hexdigest()
        with patch('aptem.services.review_pdf_transfer.ContainerClient') as factory:
            client = factory.from_container_url.return_value
            blob = client.get_blob_client.return_value
            blob.upload_blob.side_effect = ResourceExistsError('exists')
            blob.get_blob_properties.return_value = MagicMock(size=len(body),metadata={'sha256':digest},etag='etag')
            blob.download_blob.return_value.chunks.return_value = [body]
            result = ReviewBlobStore().store(document(),io.BytesIO(body),len(body),digest)
            self.assertFalse(blob.upload_blob.call_args.kwargs['overwrite'])
            self.assertNotIn('SECRET',str(result))
            self.assertNotIn('?',result['azure_account_url'])
            self.assertEqual(result['sha256'],digest)

    def test_bad_cloud_bytes_are_not_marked_verified(self):
        body = b'%PDF-1.7\noriginal\n%%EOF'
        digest = hashlib.sha256(body).hexdigest()
        with patch('aptem.services.review_pdf_transfer.ContainerClient') as factory:
            blob = factory.from_container_url.return_value.get_blob_client.return_value
            blob.get_blob_properties.return_value = MagicMock(size=len(body),metadata={'sha256':digest})
            blob.download_blob.return_value.chunks.return_value = [b'bad']
            with self.assertRaisesRegex(TransferError,'checksum'):
                ReviewBlobStore().store(document(),io.BytesIO(body),len(body),digest)

    def test_public_container_is_rejected(self):
        with patch('aptem.services.review_pdf_transfer.ContainerClient') as factory, \
                patch('aptem.services.review_pdf_transfer.requests.get') as get:
            probe = factory.from_container_url.return_value.get_blob_client.return_value
            probe.download_blob.return_value.readall.return_value = b'review-pdf-archive-health-check-v1\n'
            probe.url = 'https://example.blob.core.windows.net/reviews/health?sig=SECRET'
            get.return_value.__enter__.return_value.status_code = 200
            with self.assertRaisesRegex(TransferError,'public access'):
                ReviewBlobStore().preflight()

    def test_service_sas_preflight_checks_blobs_without_container_properties(self):
        with patch('aptem.services.review_pdf_transfer.ContainerClient') as factory, \
                patch('aptem.services.review_pdf_transfer.requests.get') as get:
            client = factory.from_container_url.return_value
            probe = client.get_blob_client.return_value
            probe.download_blob.return_value.readall.return_value = b'review-pdf-archive-health-check-v1\n'
            probe.url = 'https://example.blob.core.windows.net/reviews/health?sig=SECRET'
            get.return_value.__enter__.return_value.status_code = 404
            self.assertTrue(ReviewBlobStore().preflight())
            client.get_container_properties.assert_not_called()
            self.assertNotIn('SECRET',get.call_args.args[0])


@override_settings(APTEM_LOCAL_AUTH_BYPASS=True)
class DocumentPermissionsTests(SimpleTestCase):
    def test_local_bypass_does_not_expose_signed_documents(self):
        request = APIRequestFactory().get('/api/evidence-tool/learners/12/review-documents/')
        response = learner_documents(request,12)
        self.assertIn(response.status_code,(401,403))

    def test_nonstaff_cannot_download(self):
        request = APIRequestFactory().get('/api/evidence-tool/review-documents/1/file/')
        force_authenticate(request,user=MagicMock(is_authenticated=True,is_staff=False))
        response = download_document(request,1)
        self.assertEqual(response.status_code,403)


@override_settings(REVIEW_PDF_MAX_ATTEMPTS=5)
class CoordinatorRecoveryTests(SimpleTestCase):
    def coordinator(self, deadline=math.inf):
        command = Command()
        command.abort = threading.Event()
        command.mutex = threading.Lock()
        command.cooldown_until = 0.0
        command.last_transient_error = None
        command.blocker = None
        command.deadline = deadline
        command.stop_path = MagicMock()
        command.stop_path.exists.return_value = False
        command.checkpoint = MagicMock()
        command.stdout = MagicMock()
        return command

    def test_azure_network_outage_pauses_instead_of_aborting_or_exhausting_attempts(self):
        command = self.coordinator()
        error = TransferError('Azure HTTP network: unknown',retryable=True,global_block=True)
        with patch('aptem.management.commands.sync_review_pdfs.time.monotonic',return_value=100):
            self.assertTrue(command.failure_policy(error,99))
        self.assertEqual(command.cooldown_until,400)
        self.assertEqual(command.last_transient_error,str(error))
        self.assertFalse(command.abort.is_set())
        self.assertIsNone(command.blocker)

    def test_azure_permission_failure_still_blocks(self):
        command = self.coordinator()
        error = TransferError('Azure HTTP 403: AuthorizationFailure',global_block=True)
        self.assertFalse(command.failure_policy(error,1))
        self.assertTrue(command.abort.is_set())
        self.assertEqual(command.blocker,str(error))

    def test_lost_lease_stops_workers(self):
        command = self.coordinator()
        self.assertFalse(command.failure_policy(LeaseLost('coordinator lease was lost'),1))
        self.assertTrue(command.abort.is_set())

    def test_no_deadline_run_retries_known_temporary_file_failures(self):
        command = self.coordinator()
        self.assertTrue(command.failure_policy(TransferError('PDF download interrupted',retryable=True),99))
        self.assertFalse(command.abort.is_set())

    def test_bounded_run_retains_local_retry_limit(self):
        command = self.coordinator(deadline=1e12)
        error = TransferError('PDF download interrupted',retryable=True)
        self.assertTrue(command.failure_policy(error,4))
        self.assertFalse(command.failure_policy(error,5))

    def test_permanent_files_and_unknown_errors_are_not_retried_forever(self):
        command = self.coordinator()
        self.assertFalse(command.failure_policy(TransferError('Unable to parse original PDF'),1))
        self.assertFalse(command.failure_policy(ValueError('unknown'),5))
        self.assertFalse(command.abort.is_set())

    def test_no_deadline_still_respects_manual_stop(self):
        command = self.coordinator()
        self.assertFalse(command.stopping())
        command.stop_path.exists.return_value = True
        self.assertTrue(command.stopping())

    def test_bounded_deadline_is_still_enforced(self):
        command = self.coordinator(deadline=100)
        with patch('aptem.management.commands.sync_review_pdfs.time.monotonic',return_value=101):
            self.assertTrue(command.stopping())

    def test_startup_retries_transient_storage_outage(self):
        command = self.coordinator()
        clock = [100]
        def advance(_):
            clock[0] += 301
        with patch('aptem.management.commands.sync_review_pdfs.ReviewBlobStore') as store, \
                patch('aptem.management.commands.sync_review_pdfs.time.monotonic',side_effect=lambda: clock[0]), \
                patch.object(command.abort,'wait',side_effect=advance):
            store.return_value.preflight.side_effect = [
                TransferError('Azure temporary network outage',retryable=True,global_block=True),True]
            self.assertTrue(command.preflight())
            self.assertEqual(store.return_value.preflight.call_count,2)
            self.assertEqual(store.return_value.close.call_count,2)
        command.checkpoint.assert_called_with('waiting_for_storage')
        self.assertFalse(command.abort.is_set())

    def test_startup_does_not_retry_permission_or_privacy_failure(self):
        command = self.coordinator()
        with patch('aptem.management.commands.sync_review_pdfs.ReviewBlobStore') as store:
            store.return_value.preflight.side_effect = TransferError('Reviews container permits public access')
            with self.assertRaisesRegex(TransferError,'public access'):
                command.preflight()
            self.assertEqual(store.return_value.preflight.call_count,1)
            store.return_value.close.assert_called_once()

    def test_azure_error_classification_keeps_credentials_out_of_logs(self):
        for status,retryable in ((None,True),(429,True),(503,True),(403,False)):
            exc = MagicMock(status_code=status,error_code='AuthorizationFailure')
            error = azure_error(exc)
            self.assertEqual(error.retryable,retryable)
            self.assertTrue(error.global_block)


class LeaseTests(SimpleTestCase):
    def test_live_lease_cannot_be_replaced(self):
        with patch('aptem.services.review_pdf_lease.connection') as db:
            cursor = db.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = None
            self.assertFalse(ReviewArchiveLease().acquire())
            self.assertIn('WHERE current_lease.expires_at<=now()',cursor.execute.call_args.args[0])

    def test_expired_lease_can_be_claimed(self):
        with patch('aptem.services.review_pdf_lease.connection') as db:
            cursor = db.cursor.return_value.__enter__.return_value
            lease = ReviewArchiveLease()
            cursor.fetchone.return_value = (lease.owner,)
            self.assertTrue(lease.acquire())

    def test_old_owner_cannot_renew_or_release_a_new_lease(self):
        with patch('aptem.services.review_pdf_lease.connection') as db:
            cursor = db.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = None
            lease = ReviewArchiveLease()
            with self.assertRaises(LeaseLost):
                lease.renew()
            sql,params = cursor.execute.call_args.args
            self.assertIn('owner_token=%s::uuid AND expires_at>now()',sql)
            self.assertEqual(params[-1],lease.owner)
            lease.release()
            sql,params = cursor.execute.call_args.args
            self.assertIn('owner_token=%s::uuid',sql)
            self.assertEqual(params[-1],lease.owner)

    def test_checkpoints_abort_immediately_on_lease_failure(self):
        command = Command()
        command.lease = MagicMock()
        command.lease.renew.side_effect = LeaseLost('lease expired')
        command.abort = threading.Event()
        with self.assertRaises(LeaseLost):
            command.checkpoint('running')
        self.assertTrue(command.abort.is_set())
