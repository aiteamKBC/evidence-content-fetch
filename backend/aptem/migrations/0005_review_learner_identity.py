from django.db import migrations


def add_learner_identity(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SELECT to_regclass(%s)', ['"Learner".reviews'])
        if cursor.fetchone()[0] is None:
            return
        cursor.execute('''ALTER TABLE "Learner".reviews
            ADD COLUMN IF NOT EXISTS learner_name text,
            ADD COLUMN IF NOT EXISTS learner_email text''')
        cursor.execute('''UPDATE "Learner".reviews r
            SET learner_name=l.full_name, learner_email=l.email
            FROM "Learner".learners l WHERE l.id=r.learner_id
            AND (r.learner_name IS DISTINCT FROM l.full_name
                 OR r.learner_email IS DISTINCT FROM l.email)''')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0004_ensure_fetching_evidence_processing_fk')]
    operations = [migrations.RunPython(add_learner_identity, migrations.RunPython.noop)]
