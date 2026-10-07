"""Make Aptem's global review identity safe for concurrent upserts."""
from django.db import migrations


def add_unique_review_identity(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS learner_reviews_aptem_review_id_uniq
            ON "Learner".reviews(aptem_review_id) WHERE aptem_review_id IS NOT NULL''')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0007_review_pdf_sync_lease')]
    operations = [migrations.RunPython(add_unique_review_identity, migrations.RunPython.noop)]
