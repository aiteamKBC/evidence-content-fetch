import logging
import tempfile
from io import BytesIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from docx import Document
from pypdf import PdfWriter
from rest_framework.test import APIClient

from aptem.models import AptemEvidence
from aptem.services.evidence_analyser import normalize_analysis
from aptem.services.aptem_auth import decode_jwt_expiry, extract_csrf_token, write_env_value
from aptem.services.evidence_downloader import (
    AptemDownloadError,
    AptemEvidenceDownloader,
    DownloadResult,
    build_document_url,
    extract_filename,
    filename_for_response,
)
from aptem.services.processor import _apply_quality_check, process_evidence_batch
from aptem.services.text_extractor import count_words, extract_text


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        content=b"hello",
        content_type="text/plain",
        content_disposition='attachment; filename="evidence.txt"',
        url="https://kentbusinesscollege.aptem.co.uk/MWS.LearningProgrammes/LearningItemEvidence/Document/507",
    ):
        self.status_code = status_code
        self.content = content
        self.headers = {
            "Content-Type": content_type,
            "Content-Disposition": content_disposition,
        }
        self.url = url

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]


@override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(),
    APTEM_COOKIE_HEADER="AUTHTOKEN=test; ASP.NET_SessionId=test; __RequestVerificationToken=test",
    APTEM_DOWNLOAD_DIRECTORY="media/aptem_evidence",
    APTEM_MAX_RETRIES=2,
    APTEM_REQUEST_TIMEOUT_SECONDS=1,
    APTEM_REQUEST_DELAY_SECONDS=0,
    OPENAI_API_KEY="",
    OPENAI_MODEL="",
    APTEM_LOGIN_EMAIL="",
    APTEM_LOGIN_PASSWORD="",
    APTEM_AUTO_REFRESH_COOKIE=False,
)
class EvidenceDownloaderTests(TestCase):
    def downloader_for(self, *responses):
        session = mock.Mock()
        session.get.side_effect = responses
        return AptemEvidenceDownloader(session=session), session

    def test_successful_pdf_download(self):
        downloader, _ = self.downloader_for(
            FakeResponse(content=b"%PDF-1.4\n%%EOF", content_type="application/pdf")
        )

        result = downloader.download(507)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "application/pdf")
        self.assertGreater(result.file_size_bytes, 0)
        self.assertTrue(default_storage.exists(result.storage_name))

    def test_docx_download(self):
        downloader, _ = self.downloader_for(
            FakeResponse(
                content=b"docx-bytes",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                content_disposition='attachment; filename="evidence.docx"',
            )
        )

        result = downloader.download(507)

        self.assertEqual(result.filename, "evidence.docx")

    def test_txt_download(self):
        downloader, _ = self.downloader_for(FakeResponse(content=b"plain evidence"))

        result = downloader.download(507)

        self.assertTrue(result.filename.startswith("evidence"))
        self.assertTrue(result.filename.endswith(".txt"))
        self.assertEqual(result.file_size_bytes, len(b"plain evidence"))

    def test_html_login_page_response(self):
        html = b'<html><form><input name="userNameOrEmail"><input name="password"></form></html>'
        downloader, _ = self.downloader_for(FakeResponse(content=html, content_type="text/html"))

        with self.assertRaises(AptemDownloadError) as ctx:
            downloader.download(507)

        self.assertTrue(ctx.exception.authentication_failed)

    def test_401_response(self):
        downloader, _ = self.downloader_for(FakeResponse(status_code=401))

        with self.assertRaises(AptemDownloadError) as ctx:
            downloader.download(507)

        self.assertTrue(ctx.exception.authentication_failed)

    def test_403_response(self):
        downloader, _ = self.downloader_for(FakeResponse(status_code=403))

        with self.assertRaises(AptemDownloadError) as ctx:
            downloader.download(507)

        self.assertTrue(ctx.exception.authentication_failed)

    def test_404_response(self):
        downloader, _ = self.downloader_for(FakeResponse(status_code=404))

        with self.assertRaises(AptemDownloadError) as ctx:
            downloader.download(507)

        self.assertFalse(ctx.exception.authentication_failed)
        self.assertEqual(ctx.exception.status_code, 404)

    @mock.patch("aptem.services.evidence_downloader.time.sleep")
    def test_timeout(self, _sleep):
        downloader, session = self.downloader_for(
            requests.Timeout("slow"),
            requests.Timeout("slow"),
            requests.Timeout("slow"),
        )

        with self.assertRaises(AptemDownloadError):
            downloader.download(507)

        self.assertEqual(session.get.call_count, 3)

    @mock.patch("aptem.services.evidence_downloader.time.sleep")
    def test_retryable_server_error(self, _sleep):
        downloader, session = self.downloader_for(
            FakeResponse(status_code=500),
            FakeResponse(content=b"ok", content_type="text/plain"),
        )

        result = downloader.download(507)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.get.call_count, 2)

    def test_filename_extraction(self):
        self.assertEqual(extract_filename('attachment; filename="example.pdf"'), "example.pdf")

    def test_utf8_filename(self):
        header = "attachment; filename*=UTF-8''example%20file.pdf"
        self.assertEqual(extract_filename(header), "example file.pdf")

    def test_duplicate_filename(self):
        downloader, _ = self.downloader_for(
            FakeResponse(content=b"one", content_disposition='attachment; filename="same.txt"'),
            FakeResponse(content=b"two", content_disposition='attachment; filename="same.txt"'),
        )

        first = downloader.download(507)
        second = downloader.download(507)

        self.assertNotEqual(first.storage_name, second.storage_name)

    def test_generated_filename(self):
        filename = filename_for_response(507, "application/pdf", "")
        self.assertEqual(filename, "evidence_507.pdf")

    def test_invalid_hostname(self):
        with self.assertRaises(ValueError):
            build_document_url(
                507,
                "https://evil.example/MWS.LearningProgrammes/LearningItemEvidence/Document/507",
            )

    def test_invalid_url_path(self):
        with self.assertRaises(ValueError):
            build_document_url(507, "https://kentbusinesscollege.aptem.co.uk/other/507")

    def test_cookie_not_exposed_in_logs(self):
        logger = logging.getLogger("aptem.services.evidence_downloader")
        with self.assertNoLogs(logger, level="INFO"):
            downloader, _ = self.downloader_for(FakeResponse(status_code=404))
            with self.assertRaises(AptemDownloadError):
                downloader.download(507)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TextExtractionTests(TestCase):
    def test_word_count(self):
        self.assertEqual(count_words("One two, three.\nK1 S2"), 5)

    def test_txt_extraction(self):
        storage_name = default_storage.save("sample.txt", ContentFile(b"alpha beta gamma"))

        result = extract_text(storage_name)

        self.assertTrue(result["text_extracted"])
        self.assertEqual(result["word_count"], 3)

    def test_docx_extraction(self):
        document = Document()
        document.add_paragraph("alpha beta")
        buffer = BytesIO()
        document.save(buffer)
        storage_name = default_storage.save("sample.docx", ContentFile(buffer.getvalue()))

        result = extract_text(storage_name)

        self.assertTrue(result["text_extracted"])
        self.assertEqual(result["word_count"], 2)

    def test_pdf_without_extractable_text(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buffer = BytesIO()
        writer.write(buffer)
        storage_name = default_storage.save("blank.pdf", ContentFile(buffer.getvalue()))

        result = extract_text(storage_name)

        self.assertFalse(result["text_extracted"])
        self.assertEqual(result["word_count"], 0)
        self.assertEqual(result["processing_note"], "The PDF may be scanned or image based.")


class AnalysisTests(TestCase):
    def test_reflection_analysis_result(self):
        result = normalize_analysis(
            {
                "has_reflection": True,
                "reflection_strength": "moderate",
                "reflection_evidence": ["I learned from the challenge."],
            },
            "I learned from the challenge.",
        )

        self.assertTrue(result["has_reflection"])
        self.assertEqual(result["reflection_strength"], "moderate")

    def test_ksb_analysis_result(self):
        result = normalize_analysis(
            {
                "has_ksbs": True,
                "has_explicit_ksbs": True,
                "ksb_codes": ["K2", "S4", "K99"],
                "knowledge_found": True,
            },
            "The learner covers K2 and S4.",
        )

        self.assertTrue(result["has_ksbs"])
        self.assertEqual(result["ksb_detection_type"], "explicit")
        self.assertEqual(result["ksb_codes"], ["K2", "S4"])

    def test_ai_text_in_boolean_field_is_coerced_false(self):
        result = normalize_analysis(
            {
                "has_reflection": "about what marketing is",
                "has_ksbs": "no",
                "reflection_evidence": "a short quote",
                "ksb_evidence": None,
            },
            "about what marketing is",
        )

        self.assertFalse(result["has_reflection"])
        self.assertFalse(result["has_ksbs"])
        self.assertEqual(result["reflection_evidence"], ["a short quote"])
        self.assertEqual(result["ksb_evidence"], [])


@override_settings(APTEM_MIN_WORD_COUNT=500)
class QualityCheckTests(TestCase):
    def test_under_500_without_reflection_or_ksbs_needs_review(self):
        evidence = AptemEvidence(word_count=240, text_extracted=True)

        _apply_quality_check(evidence)

        self.assertTrue(evidence.is_under_word_count_threshold)
        self.assertEqual(evidence.quality_check_status, "needs_review")

    def test_under_500_with_reflection_is_acceptable(self):
        evidence = AptemEvidence(
            word_count=240,
            text_extracted=True,
            has_reflection=True,
        )

        _apply_quality_check(evidence)

        self.assertEqual(evidence.quality_check_status, "acceptable")

    def test_500_or_more_is_acceptable(self):
        evidence = AptemEvidence(word_count=500, text_extracted=True)

        _apply_quality_check(evidence)

        self.assertFalse(evidence.is_under_word_count_threshold)
        self.assertEqual(evidence.quality_check_status, "acceptable")


class AptemAuthHelperTests(TestCase):
    def test_extract_csrf_token(self):
        html = '<input name="__RequestVerificationToken" type="hidden" value="abc123">'

        self.assertEqual(extract_csrf_token(html), "abc123")

    def test_decode_jwt_expiry_invalid_token(self):
        self.assertIsNone(decode_jwt_expiry("not-a-jwt"))

    def test_write_env_value_updates_existing_key(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as handle:
            handle.write("A=1\nAPTEM_COOKIE_HEADER=old\n")
            path = handle.name

        write_env_value(path, "APTEM_COOKIE_HEADER", "new")

        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("APTEM_COOKIE_HEADER=new", content)
        self.assertNotIn("APTEM_COOKIE_HEADER=old", content)


@override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(),
    APTEM_REQUEST_DELAY_SECONDS=0,
    OPENAI_API_KEY="",
    OPENAI_MODEL="",
)
class ProcessorAndApiTests(TestCase):
    def test_sequential_batch_order(self):
        order = []

        class FakeDownloader:
            def download(self, evidence_id, document_url=None):
                order.append(evidence_id)
                storage_name = default_storage.save(f"{evidence_id}.txt", ContentFile(b"one two"))
                return DownloadResult(
                    document_url=f"https://kentbusinesscollege.aptem.co.uk/MWS.LearningProgrammes/LearningItemEvidence/Document/{evidence_id}",
                    status_code=200,
                    content_type="text/plain",
                    content_disposition=f'attachment; filename="{evidence_id}.txt"',
                    storage_name=storage_name,
                    filename=f"{evidence_id}.txt",
                    file_size_bytes=7,
                )

        records = process_evidence_batch(
            [
                {"evidence_id": 507, "evidence_name": "One"},
                {"evidence_id": 508, "evidence_name": "Two"},
            ],
            downloader=FakeDownloader(),
        )

        self.assertEqual(order, [507, 508])
        self.assertEqual([record.status for record in records], ["analysed", "analysed"])

    def test_cookie_not_exposed_in_api_output(self):
        user = get_user_model().objects.create_user(
            username="staff",
            password="pass",
            is_staff=True,
        )
        AptemEvidence.objects.create(evidence_id=507, evidence_name="Safe")
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get("/api/aptem/evidence/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("AUTHTOKEN", response.content.decode())

    def test_staff_required(self):
        user = get_user_model().objects.create_user(username="learner", password="pass")
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get("/api/aptem/evidence/")

        self.assertEqual(response.status_code, 403)
