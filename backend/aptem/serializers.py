from rest_framework import serializers

from .models import AptemEvidence


class AptemEvidenceProcessSerializer(serializers.Serializer):
    evidence_id = serializers.IntegerField(min_value=1)
    evidence_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    document_url = serializers.URLField(required=False, allow_blank=True)


class AptemEvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = AptemEvidence
        fields = [
            "id",
            "evidence_id",
            "evidence_name",
            "document_url",
            "status",
            "status_code",
            "content_type",
            "content_disposition",
            "downloaded_file_name",
            "file_size_bytes",
            "word_count",
            "character_count",
            "text_extracted",
            "extracted_text",
            "extraction_note",
            "word_count_threshold",
            "is_under_word_count_threshold",
            "quality_check_status",
            "quality_check_summary",
            "analysis_status",
            "has_reflection",
            "reflection_strength",
            "reflection_evidence",
            "has_ksbs",
            "has_explicit_ksbs",
            "has_implicit_ksbs",
            "ksb_detection_type",
            "ksb_codes",
            "knowledge_found",
            "skills_found",
            "behaviours_found",
            "ksb_evidence",
            "analysis_summary",
            "attempts",
            "failure_reason",
            "authentication_failed",
            "created_at",
            "started_at",
            "completed_at",
            "updated_at",
        ]
        read_only_fields = fields


class AptemEvidenceSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AptemEvidence
        fields = [
            "id",
            "evidence_id",
            "evidence_name",
            "status",
            "downloaded_file_name",
            "file_size_bytes",
            "word_count",
            "word_count_threshold",
            "is_under_word_count_threshold",
            "quality_check_status",
            "quality_check_summary",
            "has_reflection",
            "reflection_strength",
            "has_ksbs",
            "ksb_codes",
            "attempts",
            "failure_reason",
            "completed_at",
        ]
        read_only_fields = fields
