"""One safe reviews-sync cycle.

Layered after the pattern in the project's scheduled-task guide:

1. A database lease (the control row below, locked with SELECT ... FOR UPDATE) so
   two run_cycle invocations never overlap. The underlying commands keep their own
   locks too (sync_all_aptem_reviews: file lock; sync_review_pdfs:
   "Learner".review_pdf_sync_lease), so this is a cheap higher-level guard.
2. A window mode: every cycle is "incremental" (PDF archive scoped to active
   learners); once per 24h it runs "full" (PDF archive over all learners) to
   catch changes to older records. The reviews reconcile itself always walks the
   full live inventory and only writes reviews that need content, so it needs no
   date window.
3. Each stage runs in its own try/except on a fresh connection; one failing
   stage does not stop the rest, and the cursor only advances on a real run where
   the work that ran actually completed.

The control-row SQL is written with the schema spelled out
(fetching_evidence.aptem_cacherefreshcontrol) on purpose: the database is reached
through Neon's PgBouncer *transaction* pooler, where a session-level
`SET search_path` does not reliably survive across transactions. The ORM would
emit the bare table name and depend on that search_path, which fails
intermittently on long runs; every other part of this pipeline qualifies its
schema for the same reason.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone


LEASE = "reviews_cycle"
CURSOR = "reviews_cycle_cursor"   # start time of the last successful cycle
FULL = "reviews_cycle_full"       # last full-scope (all learners) pass

STALE_AFTER = timedelta(hours=6)  # a killed run leaves the lease; treat it dead after this
FULL_EVERY = timedelta(hours=24)  # run the full-scope PDF pass at most this often

# aptem_* tables live in the first schema of DATABASE_SEARCH_PATH (fetching_evidence).
_SCHEMA = (
    (getattr(settings, "DATABASE_SEARCH_PATH", "") or "fetching_evidence")
    .split(",")[0].strip() or "fetching_evidence"
)
TABLE = f"{_SCHEMA}.aptem_cacherefreshcontrol"


class CycleService:
    def __init__(self, *, options=None):
        self.options = options or {}

    # -- lease ---------------------------------------------------------------
    def claim(self):
        now = timezone.now()
        with transaction.atomic():
            with connection.cursor() as c:
                # last_error is NOT NULL with only a Python-level default, so a raw
                # insert must supply it explicitly.
                c.execute(
                    f"INSERT INTO {TABLE} (cache_name, last_error) VALUES (%s, '') "
                    f"ON CONFLICT (cache_name) DO NOTHING",
                    [LEASE],
                )
                c.execute(
                    f"SELECT refresh_started_at FROM {TABLE} "
                    f"WHERE cache_name=%s FOR UPDATE",
                    [LEASE],
                )
                started = c.fetchone()[0]
                if started and started > now - STALE_AFTER:
                    return False
                c.execute(
                    f"UPDATE {TABLE} SET refresh_started_at=%s WHERE cache_name=%s",
                    [now, LEASE],
                )
                return True

    def release(self, error=""):
        with connection.cursor() as c:
            c.execute(
                f"UPDATE {TABLE} SET refresh_started_at=NULL, refreshed_at=%s, "
                f"last_error=%s WHERE cache_name=%s",
                [timezone.now(), (error or "")[:2000], LEASE],
            )

    # -- window --------------------------------------------------------------
    def resolve_mode(self, now):
        with connection.cursor() as c:
            c.execute(
                f"SELECT refreshed_at FROM {TABLE} WHERE cache_name=%s", [FULL]
            )
            row = c.fetchone()
        last_full = row[0] if row else None
        if not last_full or last_full < now - FULL_EVERY:
            return "full"
        return "incremental"

    def save_cursor(self, run_started, mode):
        names = [CURSOR] + ([FULL] if mode == "full" else [])
        with connection.cursor() as c:
            for name in names:
                c.execute(
                    f"INSERT INTO {TABLE} (cache_name, refreshed_at, last_error) "
                    f"VALUES (%s, %s, '') "
                    f"ON CONFLICT (cache_name) DO UPDATE SET refreshed_at=EXCLUDED.refreshed_at",
                    [name, run_started],
                )

    # -- stages --------------------------------------------------------------
    @staticmethod
    def stage(stages, name, fn):
        # Fresh connection per stage: the previous stage may have left one idle
        # long enough for Neon to close it.
        connection.close()
        try:
            stages[name] = {"status": "completed", "result": fn()}
        except Exception as exc:  # a failed stage must not stop the rest
            stages[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    # -- cycle ---------------------------------------------------------------
    def run_once(self):
        if not self.claim():
            return {"status": "skipped_locked"}

        opts = self.options
        apply = opts.get("apply", True)
        limit = int(opts.get("limit", 0) or 0)
        workers = int(opts.get("workers", 5) or 5)
        run_hours = float(opts.get("run_hours", 8))
        skip_reviews = bool(opts.get("skip_reviews", False))
        skip_pdfs = bool(opts.get("skip_pdfs", False))

        stages = {}
        error = ""
        try:
            started = timezone.now()
            mode = self.resolve_mode(started)
            scope = opts.get("scope") or ("all" if mode == "full" else "active")

            if not skip_reviews:
                # Supplement learners from Created_users first, so any new enrolment
                # that has an Aptem id is present before the review sync maps on it.
                self.stage(
                    stages,
                    "reconcile_learners",
                    lambda: _run_command(
                        "reconcile_learners_from_created_users",
                        apply=apply,
                    ),
                )
                self.stage(
                    stages,
                    "reviews",
                    lambda: _run_command(
                        "sync_all_aptem_reviews",
                        apply=apply,
                        workers=workers,
                        limit=limit,
                    ),
                )

            if not skip_pdfs:
                self.stage(
                    stages,
                    "review_pdfs",
                    lambda: _run_command(
                        "sync_review_pdfs",
                        apply=apply,
                        scope=scope,
                        workers=workers,
                        limit=limit,
                        run_hours=run_hours,
                    ),
                )

            # Advance the cursor only on a real run where every stage that ran
            # completed. A dry run (--no-apply) wrote nothing, so it must not move
            # the window and make the next run think a full pass already happened.
            ran = [s for s in stages.values()]
            connection.close()  # the stages may have left the connection unusable
            if apply and ran and all(s["status"] == "completed" for s in ran):
                self.save_cursor(started, mode)
            else:
                error = "; ".join(
                    f"{name}: {s['error']}"
                    for name, s in stages.items()
                    if s["status"] == "failed"
                )

            return {"status": "completed", "mode": mode, "scope": scope, "stages": stages}
        finally:
            connection.close()  # force a clean connection for the lease release
            self.release(error)


def _run_command(name, **kwargs):
    """Call a management command and return its captured stdout tail.

    call_command maps keyword args to the command's option dests
    (apply, workers, limit, scope, run_hours).
    """
    import io

    buffer = io.StringIO()
    call_command(name, stdout=buffer, stderr=buffer, **kwargs)
    output = buffer.getvalue().strip().splitlines()
    return {"stdout_tail": output[-5:]}
