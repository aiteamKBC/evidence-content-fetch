"""A renewable coordinator lease that also works with transaction pooling."""
from django.db import migrations


def create_lease(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('''CREATE TABLE "Learner".review_pdf_sync_lease (
            name text PRIMARY KEY,
            owner_token uuid NOT NULL,
            expires_at timestamptz NOT NULL,
            heartbeat_at timestamptz NOT NULL DEFAULT now()
        )''')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0006_review_documents')]
    operations = [migrations.RunPython(create_lease)]
