"""Local, additive visible-review refresh. Never invokes hidden-row cleanup."""
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from django.db import connection

from aptem.management.commands.sync_visible_aptem_reviews import sync_selected, verify_saved_visible
from aptem.services.classic_review_inventory import classic_visible_ids
from aptem.services.review_api_sync import api_get, canonical_mapping
from aptem.services.review_probe import aptem_page
from aptem.services.review_visibility import reconcile_visibility
from aptem.services.review_signature_completion import CompletionClient
from aptem.services.review_signatures import (
    apply_rows, classify, inventory, load_verified, write_verified,
)


def now():
    return datetime.now(timezone.utc).isoformat()


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def local_lock(path):
    """OS lock releases on process exit; a leftover file is not a stale lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(str(path)) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def save(path, value):
    # Windows antivirus/editor readers can briefly prevent atomic replacement.
    for attempt in range(6):
        try:
            write_verified(path, value)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.2 * 2 ** attempt)


def scope():
    mapping, fallback = canonical_mapping()
    with connection.cursor() as cursor:
        cursor.execute('''SELECT DISTINCT learner_id FROM "Learner".reviews
            WHERE review_data->>'synthetic_lms_validation'='true' ''')
        synthetic = {row[0] for row in cursor.fetchall()}
    # Signature compare-and-set requires a persisted canonical Aptem identity.
    if fallback:
        raise ValueError('Canonical Aptem IDs need reconciliation before scheduled refresh')
    return {key: value for key, value in mapping.items()
            if value not in synthetic and str(key).isdecimal()}


def backup(canonical_id, ids, folder):
    """Full before-images, including section IDs, verified BEFORE the first write."""
    with connection.cursor() as cursor:
        cursor.execute('''SELECT to_jsonb(r) FROM "Learner".reviews r
            WHERE learner_id=%s AND aptem_review_id=ANY(%s) ORDER BY id''', [canonical_id, ids])
        reviews = [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in cursor.fetchall()]
        cursor.execute('''SELECT to_jsonb(s) FROM "Learner".review_sections s
            JOIN "Learner".reviews r ON r.id=s.review_id
            WHERE r.learner_id=%s AND r.aptem_review_id=ANY(%s) ORDER BY s.id''', [canonical_id, ids])
        sections = [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in cursor.fetchall()]
    value = {'created_at': now(), 'canonical_id': canonical_id, 'visible_ids': ids,
             'reviews': reviews, 'review_sections': sections}
    save(folder/'before.json', value)
    return load_verified(folder/'before.json')


def refresh_signatures(learner_id, ids, folder):
    rows = [r for r in inventory(learner_id) if r['aptem_review_id'] in set(ids)]
    if not rows:
        return {'updated': 0, 'states': {}, 'errors': {}}
    client = CompletionClient()
    errors, prepared = {}, []
    try:
        evidence = client.learner(learner_id)
        save(folder/'signature-source.json', evidence)
        for row in rows:
            try:
                result = classify(row['aptem_review_id'], learner_id, evidence)
                if not result['verified']:
                    programs = sorted({g['ProgramId'] for g in evidence['groups']
                                      if str(g.get('ItemId')) == row['aptem_review_id']})
                    data = api_get(client.session, '/odata/1.0/Reviews', params={
                        '$filter': 'Id eq ' + row['aptem_review_id'], '$top': 2,
                        '$select': 'Id,LearnerId,OwnerId,OwnerName,Status'}, timeout=60).json()
                    metadata = data.get('value')
                    if (not isinstance(metadata, list) or len(metadata) > 1
                            or any(str(m.get('Id')) != row['aptem_review_id']
                                   or str(m.get('LearnerId')) != str(learner_id) for m in metadata)):
                        raise ValueError('Signature owner metadata identity mismatch')
                    # Resolve needs a classification even for a newly imported review.
                    value = client.resolve({**row, 'before': result},
                                           metadata[0] if metadata else None, programs, folder)
                    save(folder/('signature-'+row['aptem_review_id']+'.json'), value)
                    result = value['after']
                if not result['verified']:
                    raise ValueError(result.get('reason', 'Signature verification incomplete'))
                row['after'] = result
                prepared.append(row)
            except Exception as exc:
                # Keep previous verified metadata on source failure; expose stale/error in report.
                errors[row['aptem_review_id']] = type(exc).__name__ + ': ' + str(exc)[:350]
        save(folder/'signature-recovery.json', {'rows': prepared})
        saved = load_verified(folder/'signature-recovery.json')['rows']
        changed = apply_rows(saved) if saved else 0
        return {'updated': changed, 'states': dict(Counter(r['after']['state'] for r in saved)), 'errors': errors}
    finally:
        client.close()


def sync_learner(learner_id, canonical_id, folder):
    folder.mkdir(parents=True, exist_ok=True)
    with aptem_page() as page:
        page.set_default_timeout(45000)
        page.set_default_navigation_timeout(90000)
        ids, _, _ = classic_visible_ids(page, learner_id, include_withdrawn=True)
    ids = [str(value) for value in ids]
    if len(ids) != len(set(ids)) or any(not value.isdecimal() for value in ids):
        raise ValueError('Visible review inventory has invalid or duplicated identities')
    save(folder/'visible.json', {'learner_id': learner_id, 'review_ids': ids, 'checked_at': now()})
    previous = backup(canonical_id, ids, folder)
    before_ids = {str(r['aptem_review_id']) for r in previous['reviews']}
    result = {'visible': len(ids), 'new': len(set(ids)-before_ids), 'errors': {}}
    try:
        result['content'] = sync_selected(learner_id, canonical_id, ids, classic_only=True, refresh_visible_panels=True)
    except Exception as exc:
        # Retain successful per-review transactions, then retry each selected ID independently.
        result['initial_content_error'] = type(exc).__name__
        for review_id in ids:
            try:
                sync_selected(learner_id, canonical_id, [review_id], classic_only=True, refresh_visible_panels=True)
            except Exception as error:
                result['errors'][review_id] = type(error).__name__ + ': ' + str(error)[:350]
    try:
        verify_saved_visible(canonical_id, ids)
    except Exception as exc:
        result['errors']['content_verification'] = str(exc)[:1000]
    # Membership is independent of content/signature completeness: a visible
    # review with an Aptem content error must remain in the current list.
    try:
        result['visibility'] = reconcile_visibility(
            learner_id, canonical_id, folder/'visible.json', folder/'visibility-before.json', apply=True)
    except Exception as exc:
        result['errors']['visibility'] = type(exc).__name__ + ': ' + str(exc)[:1000]
    try:
        result['signatures'] = refresh_signatures(learner_id, ids, folder)
        if result['signatures']['errors']:
            result['errors']['signatures'] = result['signatures']['errors']
    except Exception as exc:
        result['errors']['signatures'] = type(exc).__name__ + ': ' + str(exc)[:350]
    # Verify preserved primary keys: dependent projects keep their existing links.
    if previous['reviews']:
        with connection.cursor() as cursor:
            cursor.execute('SELECT id FROM "Learner".reviews WHERE id=ANY(%s)',
                           [[r['id'] for r in previous['reviews']]])
            if {r[0] for r in cursor.fetchall()} != {r['id'] for r in previous['reviews']}:
                raise ValueError('Previously stored review identities changed')
    result.update(status='partial' if result['errors'] else 'complete', finished_at=now())
    save(folder/'result.json', result)
    return result


def run_worker(folder, index, workers, lock_root):
    mapping = load_verified(folder/'scope.json')
    selected = [(key, value) for key, value in mapping.items() if int(key) % workers == index]
    report_path = folder/f'worker-{index}.json'
    report = load_verified(report_path) if report_path.exists() else {
        'pid': os.getpid(), 'worker': index, 'total': len(selected), 'learners': {}, 'started_at': now()}
    for attempt in range(2):
        for learner_id, canonical_id in selected:
            if report['learners'].get(learner_id, {}).get('status') == 'complete':
                continue
            report.update(current=learner_id, attempt=attempt+1, updated_at=now())
            save(report_path, report)
            try:
                with local_lock(lock_root/f'learner-{learner_id}.lock'):
                    target = folder/f'worker-{index}'/learner_id/f'attempt-{attempt+1}'
                    result = sync_learner(learner_id, canonical_id, target)
            except Exception as exc:
                result = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)[:500], 'finished_at': now()}
            finally:
                connection.close()
            report['learners'][learner_id] = result
            report['updated_at'] = now()
            save(report_path, report)
            print(json.dumps({'worker': index, 'learner': learner_id, **result}), flush=True)
    report.update(current=None, finished_at=now())
    save(report_path, report)
    return any(row['status'] != 'complete' for row in report['learners'].values())
