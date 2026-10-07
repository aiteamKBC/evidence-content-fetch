from django.db import migrations


def add_source_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            alter table aptem_aptemevidence
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
            alter table aptem_aptemevidence
            drop constraint if exists aptem_aptemevidence_evidence_id_source_fk;
            """
        )


class Migration(migrations.Migration):
    dependencies = [
        ("aptem", "0002_aptemevidence_is_under_word_count_threshold_and_more"),
    ]

    operations = [
        migrations.RunPython(add_source_fk, reverse_code=drop_source_fk),
    ]
