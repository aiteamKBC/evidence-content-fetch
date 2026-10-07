from django.urls import path

from . import views
from . import review_document_views


urlpatterns = [
    path("learners/<int:learner_id>/review-documents/", review_document_views.learner_documents, name="review-document-list"),
    path("review-documents/<int:document_id>/file/", review_document_views.download_document, name="review-document-file"),
    path("auth/status/", views.auth_status, name="aptem-auth-status"),
    path("auth/login/", views.auth_login, name="aptem-auth-login"),
    path("evidence/source-items/", views.source_items, name="aptem-source-items"),
    path("evidence/source-items/<int:evidence_id>/process/", views.process_source_item, name="aptem-source-item-process"),
    path("evidence/", views.AptemEvidenceListView.as_view(), name="aptem-evidence-list"),
    path("evidence/process/", views.process_evidence, name="aptem-evidence-process"),
    path("evidence/process-batch/", views.process_evidence_batch, name="aptem-evidence-process-batch"),
    path("evidence/process-source-next/", views.process_source_next, name="aptem-evidence-process-source-next"),
    path("evidence/<int:pk>/", views.AptemEvidenceDetailView.as_view(), name="aptem-evidence-detail"),
    path("evidence/<int:pk>/retry/", views.retry_evidence, name="aptem-evidence-retry"),
    path("evidence/<int:pk>/file/", views.download_evidence_file, name="aptem-evidence-file"),
]
