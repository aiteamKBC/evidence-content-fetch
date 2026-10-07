from django.db import transaction
from django.conf import settings
from django.utils import timezone

from aptem.models import AptemEvidence

from .evidence_analyser import analyse_evidence
from .evidence_downloader import AptemDownloadError, AptemEvidenceDownloader, wait_between_evidence_files
from .text_extractor import extract_text


def process_evidence_payload(payload, *, downloader=None):
    evidence_id = payload["evidence_id"]
    defaults = {
        "evidence_name": payload.get("evidence_name", ""),
        "document_url": payload.get("document_url", ""),
    }
    evidence, _ = AptemEvidence.objects.update_or_create(
        evidence_id=evidence_id,
        defaults=defaults,
    )
    return process_evidence_record(evidence, downloader=downloader)


def process_evidence_record(evidence, *, downloader=None):
    downloader = downloader or AptemEvidenceDownloader()
    started_at = timezone.now()
    evidence.status = AptemEvidence.STATUS_PROCESSING
    evidence.started_at = started_at
    evidence.completed_at = None
    evidence.failure_reason = ""
    evidence.authentication_failed = False
    evidence.quality_check_status = "not_checked"
    evidence.quality_check_summary = ""
    evidence.attempts += 1
    evidence.save(
        update_fields=[
            "status",
            "started_at",
            "completed_at",
            "failure_reason",
            "authentication_failed",
            "quality_check_status",
            "quality_check_summary",
            "attempts",
            "updated_at",
        ]
    )

    try:
        download = downloader.download(evidence.evidence_id, evidence.document_url)
        evidence.document_url = download.document_url
        evidence.status = AptemEvidence.STATUS_DOWNLOADED
        evidence.status_code = download.status_code
        evidence.content_type = download.content_type
        evidence.content_disposition = download.content_disposition
        evidence.downloaded_file = download.storage_name
        evidence.downloaded_file_name = download.filename
        evidence.file_size_bytes = download.file_size_bytes

        extraction = extract_text(download.storage_name)
        evidence.extracted_text = extraction["text"]
        evidence.word_count = extraction["word_count"]
        evidence.character_count = extraction["character_count"]
        evidence.text_extracted = extraction["text_extracted"]
        evidence.extraction_note = extraction["processing_note"] or ""

        should_analyse_short_evidence = (
            extraction["text_extracted"]
            and evidence.word_count is not None
            and evidence.word_count < settings.APTEM_MIN_WORD_COUNT
        )
        if should_analyse_short_evidence:
            analysis = analyse_evidence(extraction["text"])
        else:
            analysis = {
                "analysis_status": "skipped",
                "analysis_summary": (
                    f"AI review skipped because the evidence has {evidence.word_count or 0} "
                    f"words and the short-evidence threshold is {settings.APTEM_MIN_WORD_COUNT}."
                ),
            }
        _apply_analysis(evidence, analysis)
        _apply_quality_check(evidence)
        evidence.status = AptemEvidence.STATUS_ANALYSED
        evidence.completed_at = timezone.now()
        evidence.save()
    except AptemDownloadError as exc:
        evidence.status = (
            AptemEvidence.STATUS_AUTHENTICATION_FAILED
            if exc.authentication_failed
            else AptemEvidence.STATUS_FAILED
        )
        evidence.status_code = exc.status_code
        evidence.authentication_failed = exc.authentication_failed
        evidence.failure_reason = str(exc)
        evidence.completed_at = timezone.now()
        evidence.save()
    except ValueError as exc:
        evidence.status = AptemEvidence.STATUS_FAILED
        evidence.failure_reason = str(exc)
        evidence.completed_at = timezone.now()
        evidence.save()
    except Exception as exc:
        evidence.status = AptemEvidence.STATUS_FAILED
        evidence.failure_reason = f"Unexpected processing error: {exc}"
        evidence.completed_at = timezone.now()
        evidence.save()
    return evidence


def process_evidence_batch(items, *, downloader=None):
    results = []
    downloader = downloader or AptemEvidenceDownloader()
    for index, payload in enumerate(items):
        with transaction.atomic():
            result = process_evidence_payload(payload, downloader=downloader)
        results.append(result)
        if index < len(items) - 1:
            wait_between_evidence_files()
    return results


def _apply_analysis(evidence, analysis):
    evidence.analysis_status = analysis.get("analysis_status", "pending")
    evidence.has_reflection = analysis.get("has_reflection", False)
    evidence.reflection_strength = analysis.get("reflection_strength", "none")
    evidence.reflection_evidence = analysis.get("reflection_evidence", [])
    evidence.has_ksbs = analysis.get("has_ksbs", False)
    evidence.has_explicit_ksbs = analysis.get("has_explicit_ksbs", False)
    evidence.has_implicit_ksbs = analysis.get("has_implicit_ksbs", False)
    evidence.ksb_detection_type = analysis.get("ksb_detection_type", "none")
    evidence.ksb_codes = analysis.get("ksb_codes", [])
    evidence.knowledge_found = analysis.get("knowledge_found", False)
    evidence.skills_found = analysis.get("skills_found", False)
    evidence.behaviours_found = analysis.get("behaviours_found", False)
    evidence.ksb_evidence = analysis.get("ksb_evidence", [])
    evidence.analysis_summary = analysis.get("analysis_summary", "")


def _apply_quality_check(evidence):
    threshold = settings.APTEM_MIN_WORD_COUNT
    word_count = evidence.word_count or 0
    evidence.word_count_threshold = threshold
    evidence.is_under_word_count_threshold = word_count < threshold

    if not evidence.text_extracted:
        evidence.quality_check_status = "needs_review"
        evidence.quality_check_summary = (
            "Text could not be extracted, so the evidence needs manual review."
        )
    elif evidence.is_under_word_count_threshold and not (
        evidence.has_reflection and evidence.has_explicit_ksbs
    ):
        evidence.quality_check_status = "needs_review"
        missing = []
        if not evidence.has_reflection:
            missing.append("Reflection")
        if not evidence.has_explicit_ksbs:
            missing.append("explicit KSBs")
        evidence.quality_check_summary = (
            f"Evidence has {word_count} words, below {threshold}, and is missing "
            f"{' and '.join(missing)}."
        )
    elif evidence.is_under_word_count_threshold:
        evidence.quality_check_status = "acceptable"
        evidence.quality_check_summary = (
            f"Evidence has {word_count} words, below {threshold}, with both Reflection and explicit KSBs found."
        )
    else:
        evidence.quality_check_status = "acceptable"
        evidence.quality_check_summary = (
            f"Evidence has {word_count} words, meeting the {threshold}-word threshold."
        )
