from django.conf import settings
from django.db import connection
import json

from aptem.models import AptemEvidence

from .processor import process_evidence_batch
from .processor import process_evidence_payload


EXCLUDED_NAME_PATTERNS = [
    "%certificate%",
    "%bil%",
    "%break in learning%",
    "%review%",
    "%hours%",
    "%welcome%",
    "%progress report%",
    "%learner_handbook%",
]


def fetch_source_evidence_items(limit=50, *, include_processed=False):
    limit = min(max(int(limit), 1), settings.APTEM_MAX_BATCH_SIZE)
    candidates = []
    with connection.cursor() as cursor:
        processed_filter = "" if include_processed else "and ae.evidence_id is null"
        cursor.execute(
            f"""
            select ei.evidence_id, ei.evidence_name, ei.source_file_url
            from fetching_evidence.evidence_items ei
            left join fetching_evidence.aptem_aptemevidence ae on ae.evidence_id = ei.evidence_id
            where ei.evidence_kind = %s
              and ei.evidence_name ilike %s
              and ei.evidence_name not ilike all(%s)
              and ei.source_file_url is not null
              and ei.source_file_url <> %s
              {processed_filter}
            order by evidence_id
            limit %s
            """,
            ["File", "%.pdf", EXCLUDED_NAME_PATTERNS, "", limit],
        )
        for evidence_id, evidence_name, source_file_url in cursor.fetchall():
            candidates.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_name": evidence_name or "",
                    "document_url": source_file_url or "",
                }
            )
    return candidates


def process_next_source_evidence(limit=50, *, include_processed=False):
    items = fetch_source_evidence_items(limit, include_processed=include_processed)
    records = process_evidence_batch(items)
    return items, records


def fetch_source_evidence_table(
    *,
    limit=50,
    offset=0,
    search="",
    processing_status="",
    quality_status="",
    under_500="",
    has_reflection="",
    has_ksbs="",
    source_status="",
):
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    params = ["File", "%.pdf", EXCLUDED_NAME_PATTERNS]
    where = [
        "ei.evidence_kind = %s",
        "ei.evidence_name ilike %s",
        "ei.evidence_name not ilike all(%s)",
    ]
    if search:
        where.append(
            "(cast(ei.evidence_id as text) ilike %s or ei.evidence_name ilike %s or ei.full_name ilike %s or ei.program_name ilike %s)"
        )
        term = f"%{search}%"
        params.extend([term, term, term, term])
    if processing_status:
        if processing_status == "not_processed":
            where.append("ae.id is null")
        else:
            where.append("ae.status = %s")
            params.append(processing_status)
    if quality_status:
        if quality_status == "not_checked":
            where.append("(ae.quality_check_status is null or ae.quality_check_status = %s)")
        else:
            where.append("ae.quality_check_status = %s")
        params.append(quality_status)
    if under_500 in {"true", "false"}:
        where.append("coalesce(ae.is_under_word_count_threshold, false) = %s")
        params.append(under_500 == "true")
    if has_reflection in {"true", "false"}:
        where.append("coalesce(ae.has_reflection, false) = %s")
        params.append(has_reflection == "true")
    if has_ksbs in {"true", "false"}:
        where.append("coalesce(ae.has_ksbs, false) = %s")
        params.append(has_ksbs == "true")
    if source_status:
        where.append("ei.evidence_status = %s")
        params.append(source_status)

    where_sql = " and ".join(where)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select
                ei.evidence_id,
                ei.learner_id,
                ei.full_name,
                ei.program_name,
                ei.evidence_kind,
                ei.evidence_name,
                ei.evidence_status,
                ei.submission_date,
                ei.completed_date,
                (ei.source_file_url is not null and ei.source_file_url <> '') as has_source_file,
                ae.id as processing_id,
                ae.status as processing_status,
                ae.downloaded_file_name,
                ae.file_size_bytes,
                ae.word_count,
                ae.word_count_threshold,
                ae.is_under_word_count_threshold,
                ae.quality_check_status,
                ae.quality_check_summary,
                ae.has_reflection,
                ae.reflection_strength,
                ae.has_ksbs,
                ae.ksb_codes,
                ae.attempts,
                ae.failure_reason,
                ae.completed_at as processed_completed_at
            from fetching_evidence.evidence_items ei
            left join fetching_evidence.aptem_aptemevidence ae on ae.evidence_id = ei.evidence_id
            where {where_sql}
            order by ei.evidence_id
            limit %s offset %s
            """,
            [*params, limit, offset],
        )
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            select count(*)
            from fetching_evidence.evidence_items ei
            left join fetching_evidence.aptem_aptemevidence ae on ae.evidence_id = ei.evidence_id
            where {where_sql}
            """,
            params,
        )
        total = cursor.fetchone()[0]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [serialize_source_row(row) for row in rows],
    }


def process_source_evidence_id(evidence_id):
    existing = AptemEvidence.objects.filter(evidence_id=evidence_id).first()
    if existing and existing.status == AptemEvidence.STATUS_ANALYSED:
        return existing

    with connection.cursor() as cursor:
        cursor.execute(
            """
            select evidence_id, evidence_name, source_file_url
            from fetching_evidence.evidence_items
            where evidence_id = %s
              and evidence_kind = %s
              and evidence_name ilike %s
              and evidence_name not ilike all(%s)
              and source_file_url is not null
              and source_file_url <> %s
            """,
            [evidence_id, "File", "%.pdf", EXCLUDED_NAME_PATTERNS, ""],
        )
        row = cursor.fetchone()

    if not row:
        raise ValueError("PDF file evidence was not found for this evidence_id.")

    return process_evidence_payload(
        {
            "evidence_id": row[0],
            "evidence_name": row[1] or "",
            "document_url": row[2] or "",
        }
    )


def serialize_source_row(row):
    return {
        "evidence_id": row["evidence_id"],
        "learner_id": row["learner_id"],
        "full_name": row["full_name"] or "",
        "program_name": row["program_name"] or "",
        "evidence_kind": row["evidence_kind"] or "",
        "evidence_name": row["evidence_name"] or "",
        "evidence_status": row["evidence_status"] or "",
        "submission_date": row["submission_date"],
        "completed_date": row["completed_date"],
        "has_source_file": bool(row["has_source_file"]),
        "processing": {
            "id": row["processing_id"],
            "status": row["processing_status"] or "not_processed",
            "downloaded_file_name": row["downloaded_file_name"] or "",
            "file_size_bytes": row["file_size_bytes"],
            "word_count": row["word_count"],
            "word_count_threshold": row["word_count_threshold"],
            "is_under_word_count_threshold": row["is_under_word_count_threshold"] or False,
            "quality_check_status": row["quality_check_status"] or "not_checked",
            "quality_check_summary": row["quality_check_summary"] or "",
            "has_reflection": row["has_reflection"] or False,
            "reflection_strength": row["reflection_strength"] or "none",
            "has_ksbs": row["has_ksbs"] or False,
            "ksb_codes": normalize_list_value(row["ksb_codes"]),
            "attempts": row["attempts"] or 0,
            "failure_reason": row["failure_reason"] or "",
            "completed_at": row["processed_completed_at"],
        },
    }


def normalize_list_value(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        return parsed if isinstance(parsed, list) else []
    return []
