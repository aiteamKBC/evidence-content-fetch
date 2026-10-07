"""Transfer original bytes; credentials never enter documents, URLs in DB, or errors."""
import hashlib
import io
import re
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.storage.blob import ContainerClient, ContentSettings
from django.conf import settings
from pypdf import PdfReader

from .aptem_auth import login_to_aptem, AptemLoginError
from .evidence_downloader import build_document_url
from .review_pdf_inventory import parse_date


class TransferError(Exception):
    def __init__(self, message, *, retryable=False, global_block=False):
        super().__init__(message)
        self.retryable = retryable
        self.global_block = global_block


class AptemSession:
    """One in-memory cookie refresh shared by the five download workers."""
    def __init__(self):
        self.cookie = settings.APTEM_COOKIE_HEADER
        self.lock = threading.Lock()
        self.last_login = 0.0

    def refresh(self, expired):
        with self.lock:
            if self.cookie != expired:
                return self.cookie
            if time.monotonic() - self.last_login < 60:
                raise TransferError('Aptem authentication cooldown', retryable=True, global_block=True)
            self.last_login = time.monotonic()
            try:
                with requests.Session() as session:
                    result = login_to_aptem(settings.APTEM_LOGIN_EMAIL, settings.APTEM_LOGIN_PASSWORD, session=session)
                if not result.login_success:
                    raise TransferError('Aptem login did not return a session', retryable=True, global_block=True)
                self.cookie = result.cookie_header
                return self.cookie
            except (AptemLoginError, requests.RequestException):
                raise TransferError('Aptem login unavailable', retryable=True, global_block=True) from None


def download_pdf(document, auth, session):
    source = build_document_url(document['source_evidence_id'], document['source_url'])
    expected = urlsplit(source)
    if expected.username or expected.password or expected.port not in (None,443):
        raise TransferError('Invalid source URL authority')
    if expected.path.rstrip('/').split('/')[-1] != str(document['source_evidence_id']):
        raise TransferError('Source URL evidence identity mismatch')
    for attempt in range(2):
        cookie = auth.cookie
        try:
            response = session.get(source, headers={'Cookie':cookie,'User-Agent':'Mozilla/5.0',
                'Accept':'application/pdf'}, stream=True, allow_redirects=False, timeout=(20,90))
        except requests.RequestException:
            raise TransferError('Aptem download connection failed', retryable=True, global_block=True) from None
        with response:
            if response.is_redirect:
                location = urlsplit(urljoin(source,response.headers.get('Location','')))
                if (location.scheme == 'https' and location.hostname == expected.hostname
                        and location.port in (None,443) and location.path.casefold() in (
                            '/users/account/logon','/users/account/accessdenied')):
                    if attempt == 0:
                        auth.refresh(cookie)
                        continue
                    raise TransferError('Aptem session expired',retryable=True,global_block=True)
                raise TransferError('Unexpected document redirect; no credentials forwarded')
            if response.status_code in (401,403):
                if attempt == 0:
                    auth.refresh(cookie)
                    continue
                raise TransferError('Aptem access denied',retryable=True,global_block=True)
            if response.status_code != 200:
                retry = response.status_code in (408,429,500,502,503,504)
                raise TransferError(f'Aptem HTTP {response.status_code}',retryable=retry,global_block=retry)
            content_type = response.headers.get('Content-Type','').lower()
            if 'html' in content_type:
                if attempt == 0:
                    auth.refresh(cookie)
                    continue
                raise TransferError('Aptem returned HTML',retryable=True,global_block=True)
            stream = tempfile.SpooledTemporaryFile(max_size=4*1024*1024,mode='w+b')
            digest = hashlib.sha256()
            size = 0
            try:
                for block in response.iter_content(65536):
                    if not block:
                        continue
                    size += len(block)
                    if size > settings.REVIEW_PDF_MAX_BYTES:
                        raise TransferError('PDF exceeds configured size limit')
                    digest.update(block)
                    stream.write(block)
                stream.seek(0)
                if stream.read(5) != b'%PDF-':
                    raise TransferError('Source did not return an original PDF')
                stream.seek(0)
                return stream, size, digest.hexdigest()
            except requests.RequestException:
                stream.close()
                raise TransferError('PDF download interrupted',retryable=True) from None
            except Exception:
                stream.close()
                raise
    raise TransferError('Aptem authentication failed',retryable=True,global_block=True)


def inspect_pdf(stream):
    try:
        stream.seek(0)
        reader = PdfReader(stream,strict=False)
        if reader.is_encrypted or not reader.pages:
            raise TransferError('PDF is encrypted or contains no readable pages')
        # Read metadata only; original file bytes and embedded signatures are preserved.
        first = '\n'.join((p.extract_text() or '') for p in reader.pages[:2])
        last = reader.pages[-1].extract_text() or ''
        dates = {}
        for label, key in (('Completed','completed_date'),('Planned','planned_date')):
            match = re.search(r'Review\s+'+label+r'\s+Date:\s*(\d{2}/\d{2}/\d{4})',first,re.I)
            dates[key] = parse_date(match[1]) if match else None
        lines = [line.strip() for line in first.splitlines() if line.strip()]
        return {'page_count':len(reader.pages),'title':lines[0] if lines else '', **dates,
            'signature_verification':'not_verified',
            'signature_labels_present':bool(re.search(r'Signature\s*:',last,re.I))}
    except TransferError:
        raise
    except Exception:
        raise TransferError('Unable to parse original PDF') from None
    finally:
        stream.seek(0)


def azure_error(exc):
    code = str(getattr(exc,'error_code','unknown')).split('.')[-1]
    code = re.sub(r'[^A-Za-z0-9_]', '', code)[:80]
    status = getattr(exc,'status_code',None)
    return TransferError(f'Azure HTTP {status or "network"}: {code}',
        retryable=status in (None,408,429,500,502,503,504), global_block=True)


class ReviewBlobStore:
    def __init__(self):
        url = settings.AZURE_REVIEWS_CONTAINER_URL
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.blob.core.windows.net'):
            raise TransferError('Configure a valid Azure reviews container URL')
        self.account_url = f'https://{parsed.hostname}'
        self.container = parsed.path.strip('/')
        if not self.container or '/' in self.container:
            raise TransferError('Azure URL must address a single container')
        try:
            self.client = ContainerClient.from_container_url(url,retry_total=2,
                connection_timeout=20,read_timeout=90,logging_enable=False)
        except (ValueError,AzureError):
            raise TransferError('Invalid Azure container configuration') from None

    def close(self):
        self.client.close()

    def preflight(self):
        # Service SAS grants blob operations, not Get Container Properties.
        # Verify write/read and privacy with a harmless, deterministic health blob.
        body = b'review-pdf-archive-health-check-v1\n'
        probe = self.client.get_blob_client('_archive-health/review-pdf-sync-v1.txt')
        try:
            try:
                probe.upload_blob(body,overwrite=False,
                    content_settings=ContentSettings(content_type='text/plain'))
            except ResourceExistsError:
                pass
            if probe.download_blob().readall() != body:
                raise TransferError('Azure health check content mismatch')
            with requests.get(probe.url.split('?',1)[0],allow_redirects=False,
                    headers={'x-ms-version':'2023-11-03'},
                    timeout=(20,30),stream=True) as anonymous:
                if anonymous.status_code == 200:
                    raise TransferError('Reviews container permits public access; private storage required')
                if anonymous.status_code not in (401,403,404):
                    raise TransferError('Could not verify private Azure document access')
            return True
        except requests.RequestException:
            raise TransferError('Azure anonymous-access check unavailable',retryable=True,global_block=True) from None
        except AzureError as exc:
            raise azure_error(exc) from None

    def store(self, document, stream, size, digest):
        blob_name = f'original-review-pdfs/learner-{document["aptem_learner_id"]}/evidence-{document["source_evidence_id"]}/{digest}.pdf'
        blob = self.client.get_blob_client(blob_name)
        try:
            stream.seek(0)
            try:
                blob.upload_blob(stream,length=size,overwrite=False,max_concurrency=1,
                    content_settings=ContentSettings(content_type='application/pdf'),
                    metadata={'sha256':digest,'aptem_learner_id':str(document['aptem_learner_id']),
                        'evidence_id':str(document['source_evidence_id'])},validate_content=True)
            except ResourceExistsError:
                pass  # Deterministic name: verify existing bytes before trusting a prior run.
            props = blob.get_blob_properties()
            if props.size != size or props.metadata.get('sha256') != digest:
                raise TransferError('Azure object metadata verification failed')
            actual = hashlib.sha256()
            for block in blob.download_blob(max_concurrency=1).chunks():
                actual.update(block)
            if actual.hexdigest() != digest:
                raise TransferError('Azure PDF checksum verification failed')
            return {'azure_account_url':self.account_url,'azure_container':self.container,
                'azure_blob_name':blob_name,'azure_etag':str(props.etag),
                'sha256':digest,'file_size':size}
        except AzureError as exc:
            raise azure_error(exc) from None

    def read(self, document):
        if document['azure_account_url'] != self.account_url or document['azure_container'] != self.container:
            raise TransferError('Archived document belongs to a different storage location')
        name = document['azure_blob_name']
        expected = f'original-review-pdfs/learner-{document["aptem_learner_id"]}/evidence-{document["source_evidence_id"]}/{document["sha256"]}.pdf'
        if name != expected or document['file_size'] > settings.REVIEW_PDF_MAX_BYTES:
            raise TransferError('Archived PDF metadata mismatch')
        try:
            blob = self.client.get_blob_client(name)
            if blob.get_blob_properties().size != document['file_size']:
                raise TransferError('Archived PDF size mismatch')
            data = blob.download_blob(max_concurrency=1).readall()
            if hashlib.sha256(data).hexdigest() != document['sha256']:
                raise TransferError('Archived PDF checksum mismatch')
            return io.BytesIO(data)
        except AzureError as exc:
            raise azure_error(exc) from None
