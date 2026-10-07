from django.db import migrations


def ensure_source_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            alter table fetching_evidence.aptem_aptemevidence
            drop constraint if exists aptem_aptemevidence_evidence_id_source_fk;
            """
        )
        cursor.execute(
            """
            alter table fetching_evidence.aptem_aptemevidence
            add constraint aptem_aptemevidence_evidence_id_source_fk
            foreign key (evidence_id)
            references fetching_evidence.evidence_items (evidence_id)
            on update cascade
            on delete restrict;
            """
        )


def drop_source_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            alter table fetching_evidence.aptem_aptemevidence
            drop constraint if exists aptem_aptemevidence_evidence_id_source_fk;
            """
        )


class Migration(migrations.Migration):
    dependencies = [
        ("aptem", "0003_link_processing_to_source_evidence_items"),
    ]

    operations = [
        migrations.RunPython(ensure_source_fk, reverse_code=drop_source_fk),
    ]
