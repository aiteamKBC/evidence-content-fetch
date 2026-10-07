"""Archive original review PDFs separately from extracted review sections."""
from django.db import migrations


def create_documents(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('''CREATE TABLE "Learner".review_documents (
            id bigserial PRIMARY KEY,
            source_evidence_id bigint NOT NULL UNIQUE,
            aptem_learner_id bigint NOT NULL,
            learner_id bigint REFERENCES "Learner".learners(id),
            review_id bigint REFERENCES "Learner".reviews(id),
            original_filename text NOT NULL,
            document_type text NOT NULL,
            source_url text NOT NULL,
            source_hash text,
            source_completed_at timestamptz,
            source_synced_at timestamptz,
            mapping_status text NOT NULL,
            review_match_status text NOT NULL DEFAULT 'pending',
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','retry','uploaded','failed')),
            attempts integer NOT NULL DEFAULT 0,
            azure_account_url text,
            azure_container text,
            azure_blob_name text,
            azure_etag text,
            sha256 text,
            file_size bigint,
            page_count integer,
            pdf_metadata jsonb NOT NULL DEFAULT '{}',
            last_error text,
            next_attempt_at timestamptz,
            started_at timestamptz,
            uploaded_at timestamptz,
            verified_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (review_id IS NULL OR learner_id IS NOT NULL),
            CHECK (status <> 'uploaded' OR (azure_blob_name IS NOT NULL AND sha256 IS NOT NULL
                AND file_size > 0 AND verified_at IS NOT NULL))
        )''')
        c.execute('CREATE INDEX review_documents_learner_idx ON "Learner".review_documents(learner_id,review_id)')
        c.execute('CREATE INDEX review_documents_queue_idx ON "Learner".review_documents(status,next_attempt_at)')
        c.execute('CREATE INDEX review_documents_aptem_idx ON "Learner".review_documents(aptem_learner_id)')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0005_review_learner_identity')]
    operations = [migrations.RunPython(create_documents)]
