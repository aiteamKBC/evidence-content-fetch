"""Durable lease and write fencing; no session state across pooled connections."""
import uuid

from django.db import connection


TABLE = '"Learner".review_pdf_sync_lease'
LEASE_NAME = 'original-review-pdf-archive'
GUARD_SQL = f'''EXISTS (SELECT 1 FROM {TABLE}
    WHERE name=%s AND owner_token=%s::uuid AND expires_at>now())'''


class LeaseLost(RuntimeError):
    pass


class ReviewArchiveLease:
    def __init__(self):
        self.owner = str(uuid.uuid4())
        self.ttl_seconds = 600

    @property
    def guard_params(self):
        return [LEASE_NAME, self.owner]

    def acquire(self):
        with connection.cursor() as cursor:
            cursor.execute(f'''INSERT INTO {TABLE} AS current_lease
                (name,owner_token,expires_at,heartbeat_at)
                VALUES (%s,%s::uuid,now()+(%s * interval '1 second'),now())
                ON CONFLICT (name) DO UPDATE SET owner_token=EXCLUDED.owner_token,
                    expires_at=EXCLUDED.expires_at,heartbeat_at=now()
                WHERE current_lease.expires_at<=now()
                RETURNING owner_token''', [*self.guard_params, self.ttl_seconds])
            return cursor.fetchone() is not None

    def renew(self):
        with connection.cursor() as cursor:
            cursor.execute(f'''UPDATE {TABLE}
                SET expires_at=now()+(%s * interval '1 second'),heartbeat_at=now()
                WHERE name=%s AND owner_token=%s::uuid AND expires_at>now()
                RETURNING owner_token''', [self.ttl_seconds, *self.guard_params])
            if cursor.fetchone() is None:
                raise LeaseLost('PDF archive coordinator lease was lost')

    def release(self):
        with connection.cursor() as cursor:
            cursor.execute(f'''UPDATE {TABLE} SET expires_at=now(),heartbeat_at=now()
                WHERE name=%s AND owner_token=%s::uuid''', self.guard_params)
