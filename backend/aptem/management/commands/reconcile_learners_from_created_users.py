"""Supplement "Learner".learners with enrolment."Created_users" rows that are
missing from it, so the review sync (which maps on learners) can reach them.

learners stays the base (it holds ~115 genuine learners that Created_users lacks);
this only ADDS the people who are in Created_users with an Aptem id but not yet in
learners. It never updates or deletes an existing learner, never touches any other
table, and is idempotent. Minimal fields only (name, email, aptem_id, programme,
status) mirroring how such learners are already stored.

    python manage.py reconcile_learners_from_created_users            # dry run (preview)
    python manage.py reconcile_learners_from_created_users --apply    # insert the rows
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction


# Candidate rows: in Created_users with a numeric Aptem id and an email, whose
# email and aptem_id are both absent from learners (so no duplicate identity).
SELECT_CANDIDATES = '''
    SELECT
        COALESCE(NULLIF(trim(cu."Username"), ''), NULLIF(trim(cu."Preferred_name"), ''), cu."Email") AS full_name,
        cu."Email" AS email,
        lower(trim(cu."Email")) AS email_normalized,
        lower(COALESCE(NULLIF(trim(cu."Programme_status"), ''), 'active')) AS lifecycle_status,
        COALESCE(cu."Programme", '') AS programme,
        COALESCE(cu."Programme_status", '') AS programme_status,
        cu."aptem_id"::bigint AS aptem_id
    FROM enrolment."Created_users" cu
    WHERE cu."aptem_id" ~ '^[0-9]+$'
      AND cu."Email" IS NOT NULL AND trim(cu."Email") <> ''
      AND NOT EXISTS (SELECT 1 FROM "Learner".learners l
                      WHERE l.email_normalized = lower(trim(cu."Email")))
      AND NOT EXISTS (SELECT 1 FROM "Learner".learners l
                      WHERE l.aptem_id = cu."aptem_id"::bigint)
    ORDER BY full_name
'''


class Command(BaseCommand):
    help = "Add learners present in Created_users but missing from Learner.learners (dry-run unless --apply)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually insert the rows (otherwise just preview).")

    def handle(self, *args, **options):
        with connection.cursor() as c:
            c.execute(SELECT_CANDIDATES)
            cols = [d[0] for d in c.description]
            rows = [dict(zip(cols, r)) for r in c.fetchall()]

        self.stdout.write("Candidates to add from Created_users -> learners: %d" % len(rows))
        for r in rows:
            self.stdout.write("  %-28s | %-40s | aptem_id=%s | %s"
                              % (r["full_name"], r["email"], r["aptem_id"], r["programme"]))

        if not options["apply"]:
            self.stdout.write("\nDRY RUN - nothing written. Re-run with --apply to insert.")
            return
        if not rows:
            self.stdout.write("Nothing to insert.")
            return

        with transaction.atomic(), connection.cursor() as c:
            # Insert straight from the candidate query so the NOT EXISTS guards are
            # evaluated atomically; columns omitted here keep their table defaults.
            # email_normalized is a generated column, so it is omitted here and
            # computed by the database from email.
            c.execute('''
                INSERT INTO "Learner".learners
                    (full_name, email, lifecycle_status,
                     programme, programme_status, aptem_id)
                ''' + '''SELECT full_name, email, lifecycle_status,
                                programme, programme_status, aptem_id
                         FROM (''' + SELECT_CANDIDATES + ''') q
                RETURNING id, full_name, aptem_id''')
            inserted = c.fetchall()
        self.stdout.write("\nInserted %d learners:" % len(inserted))
        for r in inserted:
            self.stdout.write("  id=%s | %s | aptem_id=%s" % r)
        self.stdout.write("\nRun the review sync next to pull their reviews.")
