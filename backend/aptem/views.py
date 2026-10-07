from django.conf import settings
from django.contrib.auth import authenticate, login
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AptemEvidence
from .permissions import IsAuthenticatedStaff, is_local_request
from .serializers import (
    AptemEvidenceProcessSerializer,
    AptemEvidenceSerializer,
    AptemEvidenceSummarySerializer,
)
from .services.processor import (
    process_evidence_batch as run_evidence_batch,
    process_evidence_payload,
    process_evidence_record,
)
from .services.evidence_source import process_next_source_evidence
from .services.evidence_source import fetch_source_evidence_table, process_source_evidence_id


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def auth_status(request):
    if is_local_request(request):
        return Response(
            {
                "authenticated": True,
                "is_staff": True,
                "username": "local-dev",
            }
        )
    return Response(
        {
            "authenticated": bool(request.user and request.user.is_authenticated),
            "is_staff": bool(request.user and request.user.is_authenticated and request.user.is_staff),
            "username": request.user.username if request.user and request.user.is_authenticated else "",
        }
    )


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def auth_login(request):
    username = request.data.get("username", "")
    password = request.data.get("password", "")
    user = authenticate(request, username=username, password=password)
    if not user:
        return Response({"detail": "Invalid username or password."}, status=status.HTTP_400_BAD_REQUEST)
    if not user.is_staff:
        return Response({"detail": "Staff access is required."}, status=status.HTTP_403_FORBIDDEN)
    login(request, user)
    return Response({"authenticated": True, "is_staff": True, "username": user.username})


class AptemEvidenceListView(generics.ListAPIView):
    serializer_class = AptemEvidenceSummarySerializer
    permission_classes = [IsAuthenticatedStaff]

    def get_queryset(self):
        queryset = AptemEvidence.objects.all()
        status_filter = self.request.query_params.get("status")
        evidence_id = self.request.query_params.get("evidence_id")
        has_reflection = self.request.query_params.get("has_reflection")
        has_ksbs = self.request.query_params.get("has_ksbs")

        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if evidence_id:
            queryset = queryset.filter(evidence_id=evidence_id)
        if has_reflection in {"true", "false"}:
            queryset = queryset.filter(has_reflection=has_reflection == "true")
        if has_ksbs in {"true", "false"}:
            queryset = queryset.filter(has_ksbs=has_ksbs == "true")
        return queryset


class AptemEvidenceDetailView(generics.RetrieveAPIView):
    queryset = AptemEvidence.objects.all()
    serializer_class = AptemEvidenceSerializer
    permission_classes = [IsAuthenticatedStaff]


@api_view(["GET"])
@permission_classes([IsAuthenticatedStaff])
def source_items(request):
    try:
        limit = int(request.query_params.get("limit", 50))
        offset = int(request.query_params.get("offset", 0))
    except ValueError:
        return Response(
            {"detail": "limit and offset must be numbers."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    search = request.query_params.get("search", "")
    filters = {
        "processing_status": request.query_params.get("processing_status", ""),
        "quality_status": request.query_params.get("quality_status", ""),
        "under_500": request.query_params.get("under_500", ""),
        "has_reflection": request.query_params.get("has_reflection", ""),
        "has_ksbs": request.query_params.get("has_ksbs", ""),
        "source_status": request.query_params.get("source_status", request.query_params.get("aptem_status", "")),
    }
    try:
        data = fetch_source_evidence_table(limit=limit, offset=offset, search=search, **filters)
    except Exception as exc:
        return Response(
            {"detail": "Source evidence table could not be loaded.", "error": str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    data["source_filter"] = "PDF file evidence only; notes are excluded."
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticatedStaff])
def process_source_item(request, evidence_id):
    try:
        record = process_source_evidence_id(evidence_id)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except Exception as exc:
        return Response(
            {"detail": "Source evidence processing failed.", "error": str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(AptemEvidenceSummarySerializer(record).data)


@api_view(["POST"])
@permission_classes([IsAuthenticatedStaff])
def process_evidence(request):
    serializer = AptemEvidenceProcessSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    evidence = process_evidence_payload(serializer.validated_data)
    return Response(AptemEvidenceSummarySerializer(evidence).data)


@api_view(["POST"])
@permission_classes([IsAuthenticatedStaff])
def process_evidence_batch(request):
    items = request.data.get("evidence")
    if not isinstance(items, list):
        return Response(
            {"evidence": "A list of evidence items is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(items) > settings.APTEM_MAX_BATCH_SIZE:
        return Response(
            {"evidence": f"Maximum batch size is {settings.APTEM_MAX_BATCH_SIZE}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = AptemEvidenceProcessSerializer(data=items, many=True)
    serializer.is_valid(raise_exception=True)
    records = run_evidence_batch(serializer.validated_data)
    successful = sum(record.status == AptemEvidence.STATUS_ANALYSED for record in records)
    authentication_failures = sum(
        record.status == AptemEvidence.STATUS_AUTHENTICATION_FAILED for record in records
    )
    failed = len(records) - successful
    return Response(
        {
            "total": len(records),
            "successful": successful,
            "failed": failed,
            "authentication_failures": authentication_failures,
            "results": AptemEvidenceSummarySerializer(records, many=True).data,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticatedStaff])
def process_source_next(request):
    try:
        limit = int(request.data.get("limit", 50))
    except (TypeError, ValueError):
        return Response({"limit": "Limit must be a number."}, status=status.HTTP_400_BAD_REQUEST)
    include_processed = request.data.get("include_processed", False) is True
    if limit > settings.APTEM_MAX_BATCH_SIZE:
        return Response(
            {"limit": f"Maximum batch size is {settings.APTEM_MAX_BATCH_SIZE}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        selected, records = process_next_source_evidence(
            limit,
            include_processed=include_processed,
        )
    except Exception as exc:
        return Response(
            {"detail": "Source evidence processing failed.", "error": str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    successful = sum(record.status == AptemEvidence.STATUS_ANALYSED for record in records)
    authentication_failures = sum(
        record.status == AptemEvidence.STATUS_AUTHENTICATION_FAILED for record in records
    )
    failed = len(records) - successful
    return Response(
        {
            "source_filter": "PDF file evidence only; notes are excluded.",
            "selected": selected,
            "total": len(records),
            "successful": successful,
            "failed": failed,
            "authentication_failures": authentication_failures,
            "results": AptemEvidenceSummarySerializer(records, many=True).data,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticatedStaff])
def retry_evidence(request, pk):
    evidence = get_object_or_404(AptemEvidence, pk=pk)
    evidence = process_evidence_record(evidence)
    return Response(AptemEvidenceSummarySerializer(evidence).data)


@api_view(["GET"])
@permission_classes([IsAuthenticatedStaff])
def download_evidence_file(request, pk):
    evidence = get_object_or_404(AptemEvidence, pk=pk)
    if not evidence.downloaded_file:
        return Response({"detail": "No local evidence file is available."}, status=404)
    if not evidence.downloaded_file.storage.exists(evidence.downloaded_file.name):
        return Response({"detail": "The local evidence file was not found."}, status=404)

    handle = evidence.downloaded_file.open("rb")
    response = FileResponse(handle, as_attachment=True, filename=evidence.downloaded_file_name)
    if evidence.content_type:
        response["Content-Type"] = evidence.content_type
    return response
