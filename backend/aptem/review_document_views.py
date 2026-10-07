"""Authenticated staff access; never expose container credentials to callers."""
from django.db import connection
from django.http import FileResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from .services.review_pdf_inventory import TABLE
from .services.review_pdf_transfer import ReviewBlobStore


@api_view(['GET'])
@permission_classes([IsAdminUser])
def learner_documents(request, learner_id):
    with connection.cursor() as c:
        c.execute(f'''SELECT id,review_id,original_filename,document_type,file_size,page_count,
            review_match_status,uploaded_at FROM {TABLE}
            WHERE learner_id=%s AND status='uploaded' ORDER BY source_completed_at,id''',[learner_id])
        keys = [x[0] for x in c.description]
        docs = [dict(zip(keys,row)) for row in c.fetchall()]
    return Response({'learner_id':learner_id,'documents':docs})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def download_document(request, document_id):
    with connection.cursor() as c:
        c.execute(f'''SELECT source_evidence_id,aptem_learner_id,azure_account_url,azure_container,
            azure_blob_name,sha256,file_size,original_filename FROM {TABLE}
            WHERE id=%s AND status='uploaded' AND learner_id IS NOT NULL''',[document_id])
        row = c.fetchone()
        if row is None:
            return Response({'detail':'Document not found.'},status=404)
        doc = dict(zip((x[0] for x in c.description),row))
    store = None
    try:
        store = ReviewBlobStore()
        stream = store.read(doc)
    except Exception:
        return Response({'detail':'Document storage is temporarily unavailable.'},status=503)
    finally:
        if store:
            store.close()
    response = FileResponse(stream,as_attachment=True,filename=doc['original_filename'],content_type='application/pdf')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
