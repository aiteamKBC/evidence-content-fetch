"""Create canonical learners for Aptem review owners that Neon does not know."""
import collections
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.services.review_api_sync import canonical_mapping, api_get, live_inventory, login_session


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return (value or '').strip()


class Command(BaseCommand):
    help = 'Safely reconcile review owners missing from Learner.learners.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default=str(
            settings.BASE_DIR.parent/'debug'/'unmapped-aptem-learners.json'))

    def handle(self, *args, **options):
        session = login_session()
        try:
            reviews = live_inventory(session)
            connection.close()
            mapping, _ = canonical_mapping()
            unresolved_counts = collections.Counter(
                str(row['LearnerId']) for row in reviews
                if str(row['LearnerId']) not in mapping)
            unresolved_ids = sorted(map(int, unresolved_counts), key=int)
            users = []
            for offset in range(0, len(unresolved_ids), 15):
                chunk = unresolved_ids[offset:offset+15]
                query = ' or '.join(f'Id eq {value}' for value in chunk)
                users.extend(api_get(session, '/odata/1.0/Users', params={
                    '$filter': query, '$top': '100'}).json().get('value', []))
        finally:
            session.close()
        connection.close()
        user_by_id = {int(row['Id']): row for row in users}
        missing_identity = sorted(set(unresolved_ids)-set(user_by_id))
        if missing_identity:
            raise CommandError(f'{len(missing_identity)} review owners are missing from Aptem Users')

        with connection.cursor() as cursor:
            cursor.execute('''SELECT "ID","FullName","Email","Program Name","Program-Status",
                "Start-Date","End-Date","Group","OwnerName","OwnerEmail","Learner Phone",
                "Minimum","Planned","Completed","Progress-Hours","OTJHoursStatus","Coach-RAG"
                FROM "LMS"."Aptem_users" WHERE "ID"=ANY(%s)''', [unresolved_ids])
            lms_by_id = {int(row[0]): row for row in cursor.fetchall()}
            cursor.execute('''SELECT aptem_review_id,status FROM "Learner".reviews
                WHERE aptem_review_id IS NOT NULL''')
            stored = {str(row[0]): row[1] for row in cursor.fetchall()}
        live_ids = {str(row['Id']) for row in reviews}
        # Aptem's live review IDs are numeric. Keep non-numeric local/test IDs
        # out of the source-missing reconciliation instead of trying to sort
        # them as integers (and, more importantly, never marking them absent
        # from Aptem based only on a tenant inventory comparison).
        non_numeric_stored = sorted(
            review_id for review_id in set(stored) - live_ids
            if not str(review_id).isdecimal()
        )
        historical_ids = sorted(
            (set(stored) - live_ids) - set(non_numeric_stored), key=int
        )
        if len(historical_ids) > 20:
            raise CommandError('Safety stop: unexpectedly many stored reviews are absent from Aptem')

        with connection.cursor() as cursor:
            cursor.execute('SELECT id,aptem_id,lower(trim(email)) FROM "Learner".learners')
            current = cursor.fetchall()
        email_index = collections.defaultdict(list)
        for learner_id, aptem_id, email in current:
            if email:
                email_index[email].append((learner_id, aptem_id))
        safe_existing, create = [], []
        for aptem_id in unresolved_ids:
            user = user_by_id[aptem_id]
            email = clean(user.get('Email') or user.get('PersonalEmail')).lower()
            candidates = email_index.get(email, []) if email else []
            if len(candidates) == 1 and candidates[0][1] is None:
                safe_existing.append((aptem_id, candidates[0][0]))
            elif candidates:
                raise CommandError(f'Ambiguous/conflicting canonical email for Aptem ID {aptem_id}')
            else:
                create.append(aptem_id)

        report = {
            'created_at': utcnow(), 'apply': options['apply'],
            'unresolved_learners': len(unresolved_ids),
            'unresolved_reviews': sum(unresolved_counts.values()),
            'safe_existing_links': len(safe_existing),
            'new_canonical_learners': len(create),
            'placeholder_emails': sum(
                not clean(user_by_id[value].get('Email') or user_by_id[value].get('PersonalEmail'))
                for value in create),
            'aptem_identity_rows': len(user_by_id),
            'lms_profile_rows': len(lms_by_id),
            'historical_reviews': historical_ids,
            'ignored_non_numeric_stored_reviews': non_numeric_stored,
        }
        if options['apply']:
            self.apply(safe_existing, create, user_by_id, lms_by_id, historical_ids)
            report['status'] = 'applied'
        else:
            report['status'] = 'planned'
        path = Path(options['report'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(json.dumps(report, ensure_ascii=False))

    def apply(self, safe_existing, create, users, profiles, historical_ids):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('LOCK TABLE "Learner".learners IN EXCLUSIVE MODE')
            for aptem_id, learner_id in safe_existing:
                cursor.execute('''UPDATE "Learner".learners SET aptem_id=%s,updated_at=now()
                    WHERE id=%s AND aptem_id IS NULL''', [aptem_id, learner_id])
                if cursor.rowcount != 1:
                    raise CommandError(f'Canonical learner changed while linking Aptem ID {aptem_id}')
            cursor.execute('SELECT COALESCE(MAX(id),0) FROM "Learner".learners')
            next_id = cursor.fetchone()[0] + 1
            new_emails = collections.Counter(
                clean(users[value].get('Email') or users[value].get('PersonalEmail')
                      or f'aptem-{value}@missing.invalid').lower()
                for value in create)
            duplicate_emails = [email for email, count in new_emails.items() if email and count > 1]
            if duplicate_emails:
                raise CommandError(
                    f'{len(duplicate_emails)} duplicate emails among new canonical learners')
            for aptem_id in create:
                user, profile = users[aptem_id], profiles.get(aptem_id)
                full_name = clean(user.get('FullName'))
                if not full_name:
                    raise CommandError(f'Aptem user {aptem_id} has no name')
                # Neon requires a nonblank unique email. Two historical Aptem
                # test users have no email or login, so use a deterministic,
                # explicitly non-routable placeholder rather than inventing a
                # real-looking address.
                email = clean(user.get('Email') or user.get('PersonalEmail')
                              or f'aptem-{aptem_id}@missing.invalid')
                # LMS profile indexes correspond to the SELECT in handle().
                programme = clean(profile[3]) if profile else ''
                programme_status = clean(profile[4]) if profile else clean(user.get('SubscriptionStatus'))
                values = [
                    next_id, full_name, email,
                    clean(profile[10]) if profile else '', 'historical-import', programme,
                    programme_status, clean(profile[7]) if profile else '',
                    profile[13] if profile else None, profile[11] if profile else None,
                    profile[12] if profile else None, None,
                    clean(profile[15]) if profile else '', clean(profile[8]) if profile else '',
                    clean(profile[9]) if profile else '', clean(profile[16]) if profile else '',
                    profile[5] if profile else None, profile[6] if profile else None, aptem_id,
                ]
                cursor.execute('''INSERT INTO "Learner".learners
                    (id,full_name,email,phone_number,lifecycle_status,
                     programme,programme_status,group_name,completed_hours,minimum_hours,
                     planned_hours,progress_hours,otjh_status,coach_name,coach_email,coach_rag,
                     start_date,end_date,aptem_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    values)
                next_id += 1
            for review_id in historical_ids:
                cursor.execute('''UPDATE "Learner".reviews SET
                    review_data=coalesce(review_data,'{}'::jsonb) ||
                        jsonb_build_object('source_present',false,
                            'source_missing_detected_at',%s::text,
                            'status_before_source_missing',status),
                    status='Archived',updated_at=now(),last_error=NULL
                    WHERE aptem_review_id=%s''', [utcnow(), review_id])
