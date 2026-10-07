from django.contrib import admin

from .models import AptemEvidence


@admin.register(AptemEvidence)
class AptemEvidenceAdmin(admin.ModelAdmin):
    list_display = (
        "evidence_id",
        "evidence_name",
        "status",
        "word_count",
        "has_reflection",
        "has_ksbs",
        "attempts",
        "completed_at",
    )
    list_filter = ("status", "has_reflection", "has_ksbs", "authentication_failed")
    search_fields = ("evidence_id", "evidence_name", "downloaded_file_name")
