from django.db import models


class CacheRefreshControl(models.Model):
    """One control row per named concern: the run_cycle lease, its cursor, and the
    daily full-scope marker. Shared, durable coordination state for the scheduled
    reviews sync (see aptem.services.cycle)."""

    cache_name = models.CharField(max_length=100, unique=True)
    refresh_started_at = models.DateTimeField(null=True, blank=True)  # set => a run holds the lease
    refreshed_at = models.DateTimeField(null=True, blank=True)        # last successful completion
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["cache_name"]

    def __str__(self):
        return self.cache_name


class AptemEvidence(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_DOWNLOADED = "downloaded"
    STATUS_ANALYSED = "analysed"
    STATUS_FAILED = "failed"
    STATUS_AUTHENTICATION_FAILED = "authentication_failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DOWNLOADED, "Downloaded"),
        (STATUS_ANALYSED, "Analysed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_AUTHENTICATION_FAILED, "Authentication failed"),
    ]

    REFLECTION_STRENGTH_CHOICES = [
        ("none", "None"),
        ("weak", "Weak"),
        ("moderate", "Moderate"),
        ("strong", "Strong"),
    ]

    KSB_DETECTION_CHOICES = [
        ("none", "None"),
        ("explicit", "Explicit"),
        ("implicit", "Implicit"),
        ("explicit_and_implicit", "Explicit and implicit"),
    ]

    QUALITY_CHECK_CHOICES = [
        ("not_checked", "Not checked"),
        ("acceptable", "Acceptable"),
        ("needs_review", "Needs review"),
    ]

    evidence_id = models.PositiveIntegerField(unique=True)
    evidence_name = models.CharField(max_length=255, blank=True)
    document_url = models.URLField(max_length=1000, blank=True)

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)

    status_code = models.PositiveIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    content_disposition = models.TextField(blank=True)

    downloaded_file = models.FileField(upload_to="aptem_evidence/%Y/%m/%d", blank=True)
    downloaded_file_name = models.CharField(max_length=255, blank=True)
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    word_count = models.PositiveIntegerField(null=True, blank=True)
    character_count = models.PositiveIntegerField(null=True, blank=True)
    text_extracted = models.BooleanField(default=False)
    extracted_text = models.TextField(blank=True)
    extraction_note = models.TextField(blank=True)
    word_count_threshold = models.PositiveIntegerField(default=500)
    is_under_word_count_threshold = models.BooleanField(default=False)
    quality_check_status = models.CharField(
        max_length=32,
        choices=QUALITY_CHECK_CHOICES,
        default="not_checked",
    )
    quality_check_summary = models.TextField(blank=True)

    analysis_status = models.CharField(max_length=32, default="pending")
    has_reflection = models.BooleanField(default=False)
    reflection_strength = models.CharField(
        max_length=16,
        choices=REFLECTION_STRENGTH_CHOICES,
        default="none",
    )
    reflection_evidence = models.JSONField(default=list, blank=True)

    has_ksbs = models.BooleanField(default=False)
    has_explicit_ksbs = models.BooleanField(default=False)
    has_implicit_ksbs = models.BooleanField(default=False)
    ksb_detection_type = models.CharField(
        max_length=32,
        choices=KSB_DETECTION_CHOICES,
        default="none",
    )
    ksb_codes = models.JSONField(default=list, blank=True)
    knowledge_found = models.BooleanField(default=False)
    skills_found = models.BooleanField(default=False)
    behaviours_found = models.BooleanField(default=False)
    ksb_evidence = models.JSONField(default=list, blank=True)
    analysis_summary = models.TextField(blank=True)

    attempts = models.PositiveIntegerField(default=0)
    failure_reason = models.TextField(blank=True)
    authentication_failed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.evidence_id} - {self.evidence_name or self.status}"
