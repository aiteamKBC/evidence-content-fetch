"""Persist verified Classic grid membership without deleting historical reviews."""
import json
from datetime import datetime, timezone
from pathlib import Path
from contextlib import nullcontext

from django.db import connection, transaction

from .review_signatures import load_verified, write_verified

KEY = 'aptem_visibility'


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Visibility timestamp must include a timezone')
    return parsed


def validate_snapshot(snapshot, learner_id):
    ids = snapshot.get('review_ids')
    if (str(snapshot.get('learner_id')) != str(learner_id)
            or not isinstance(ids, list)
            or any(not isinstance(v, str) or not v.isdecimal() for v in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('Invalid or mismatched visible review inventory')
    checked = timestamp(snapshot['checked_at'])
    if checked > datetime.now(timezone.utc):
        raise ValueError('Visibility inventory is dated in the future')
    return set(ids), checked


def plan_membership(rows, snapshot, learner_id):
    """Rows contain only identity and prior visibility, never content or signatures."""
    ids, checked = validate_snapshot(snapshot, learner_id)
    stored = [str(row[1]) for row in rows]
    if len(stored) != len(set(stored)):
        raise ValueError('Duplicate stored review identities')
    if ids - set(stored):
        raise ValueError('Visible reviews are missing from storage; membership left unchanged')
    changes = []
    for pk, review_id, prior in rows:
        prior = json.loads(prior) if isinstance(prior, str) else prior
        if prior is not None and timestamp(prior['checked_at']) > checked:
            raise ValueError('A newer visibility inventory is already stored')
        after = {'visible': str(review_id) in ids, 'verified': True,
                 'source': 'aptem_classic_reviews', 'checked_at': snapshot['checked_at'],
                 'aptem_learner_id': str(learner_id)}
        changes.append({'id': pk, 'aptem_review_id': str(review_id),
                        'before': prior, 'after': after})
    return changes


def reconcile_visibility(learner_id, canonical_id, snapshot_path, backup_path, *, apply=False):
    snapshot = load_verified(Path(snapshot_path))
    validate_snapshot(snapshot, learner_id)
    with (transaction.atomic() if apply else nullcontext()), connection.cursor() as cursor:
        # Protect canonical identity while tagging only this learner's records.
        cursor.execute('SELECT aptem_id FROM "Learner".learners WHERE id=%s' +
                       (' FOR SHARE' if apply else ''), [canonical_id])
        identity = cursor.fetchone()
        if not identity or str(identity[0]) != str(learner_id):
            raise ValueError('Canonical learner identity differs from visibility evidence')
        cursor.execute('''SELECT id,aptem_review_id,review_data->%s
            FROM "Learner".reviews WHERE learner_id=%s ORDER BY id''' +
            (' FOR UPDATE' if apply else ''), [KEY, canonical_id])
        changes = plan_membership(cursor.fetchall(), snapshot, learner_id)
        counts = {'visible': sum(row['after']['visible'] for row in changes),
                  'historical': sum(not row['after']['visible'] for row in changes),
                  'checked_at': snapshot['checked_at'], 'applied': apply}
        if not apply:
            return counts
        backup_path = Path(backup_path)
        if backup_path.exists():
            raise ValueError('Visibility recovery file already exists; use a new path')
        write_verified(backup_path, {'canonical_id': canonical_id, 'learner_id': learner_id,
                                    'snapshot': snapshot, 'rows': changes})
        recovery = load_verified(backup_path)
        common = {'verified': True, 'source': 'aptem_classic_reviews',
                  'checked_at': snapshot['checked_at'], 'aptem_learner_id': str(learner_id)}
        cursor.execute('''UPDATE "Learner".reviews
            SET review_data=jsonb_set(coalesce(review_data,'{}'::jsonb), ARRAY[%s]::text[],
                %s::jsonb || jsonb_build_object('visible',aptem_review_id=ANY(%s)),true)
            WHERE learner_id=%s AND id=ANY(%s)''',
            [KEY, json.dumps(common), snapshot['review_ids'], canonical_id,
             [row['id'] for row in recovery['rows']]])
        if cursor.rowcount != len(changes):
            raise ValueError('Review disappeared during visibility update')
        cursor.execute('''SELECT aptem_review_id FROM "Learner".reviews
            WHERE learner_id=%s AND review_data->%s->>'visible'='true'
              AND review_data->%s->>'verified'='true' ''', [canonical_id, KEY, KEY])
        saved = [str(row[0]) for row in cursor.fetchall()]
        if len(saved) != len(snapshot['review_ids']) or set(saved) != set(snapshot['review_ids']):
            raise ValueError('Stored visible membership differs from Aptem inventory')
        return counts


def restore_visibility(backup_path):
    """Compare-and-set rollback of this metadata key only; retain later imports."""
    recovery = load_verified(Path(backup_path))
    with transaction.atomic(), connection.cursor() as cursor:
        for row in recovery['rows']:
            cursor.execute('''UPDATE "Learner".reviews
                SET review_data=CASE WHEN %s::jsonb='null'::jsonb THEN review_data-%s
                    ELSE jsonb_set(review_data,ARRAY[%s]::text[],%s::jsonb,true) END
                WHERE id=%s AND learner_id=%s AND review_data->%s=%s::jsonb''',
                [json.dumps(row['before']), KEY, KEY, json.dumps(row['before']),
                 row['id'], recovery['canonical_id'], KEY, json.dumps(row['after'])])
            if cursor.rowcount != 1:
                raise ValueError('Visibility changed since backup; rollback refused')
    return len(recovery['rows'])


def reconcile_batch(specs, *, apply=False):
    """Small bounded batches; caller holds every learner's local worker lock."""
    snapshots = {}
    for learner_id, canonical_id, source, recovery in specs:
        snapshot = load_verified(source)
        validate_snapshot(snapshot, learner_id)
        snapshots[canonical_id] = (learner_id, snapshot, Path(recovery))
    if len(snapshots) != len(specs):
        raise ValueError('Duplicate canonical learner in visibility batch')
    if not specs:
        return {}
    with (transaction.atomic() if apply else nullcontext()), connection.cursor() as cursor:
        cursor.execute('SELECT id,aptem_id FROM "Learner".learners WHERE id=ANY(%s)' +
                       (' FOR SHARE' if apply else ''), [list(snapshots)])
        identities = {row[0]: str(row[1]) for row in cursor.fetchall()}
        if identities != {key: str(value[0]) for key, value in snapshots.items()}:
            raise ValueError('Canonical learner identities differ from visibility evidence')
        cursor.execute('''SELECT learner_id,id,aptem_review_id,review_data->%s
            FROM "Learner".reviews WHERE learner_id=ANY(%s) ORDER BY id''' +
            (' FOR UPDATE' if apply else ''), [KEY, list(snapshots)])
        rows = {key: [] for key in snapshots}
        for canonical_id, pk, review_id, prior in cursor.fetchall():
            rows[canonical_id].append((pk, review_id, prior))
        changes, results = [], {}
        for canonical_id, (learner_id, snapshot, recovery_path) in snapshots.items():
            planned = plan_membership(rows[canonical_id], snapshot, learner_id)
            results[learner_id] = {'visible': sum(row['after']['visible'] for row in planned),
                'historical': sum(not row['after']['visible'] for row in planned),
                'checked_at': snapshot['checked_at'], 'applied': apply}
            if apply:
                if recovery_path.exists():
                    raise ValueError('Visibility recovery file already exists')
                write_verified(recovery_path, {'canonical_id': canonical_id, 'learner_id': learner_id,
                    'snapshot': snapshot, 'rows': planned})
                checked = load_verified(recovery_path)
                changes.extend({'id': row['id'], 'canonical_id': canonical_id, 'value': row['after']}
                               for row in checked['rows'])
        if apply:
            cursor.execute('''UPDATE "Learner".reviews r
                SET review_data=jsonb_set(coalesce(r.review_data,'{}'::jsonb), ARRAY[%s]::text[],x.value,true)
                FROM jsonb_to_recordset(%s::jsonb) AS x(id bigint, canonical_id bigint, value jsonb)
                WHERE r.id=x.id AND r.learner_id=x.canonical_id
                RETURNING r.id,r.review_data->%s''', [KEY, json.dumps(changes), KEY])
            actual = {pk: json.loads(value) if isinstance(value, str) else value
                      for pk, value in cursor.fetchall()}
            expected = {row['id']: row['value'] for row in changes}
            if actual != expected:
                raise ValueError('Visibility readback differs from planned membership')
        return results
