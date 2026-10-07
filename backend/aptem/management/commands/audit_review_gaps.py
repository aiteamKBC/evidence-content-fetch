"""Audit review coverage: what Aptem has vs what Neon has, in one pass.

Read-only. Compares the full Aptem review inventory against stored reviews and
reports three gap classes so missing data surfaces without hunting for it:

  1. Learners not pulled at all  - Aptem reviews whose learner is not mapped to a
     canonical "Learner".learners row (unmapped). These need the learner linked
     (set aptem_id) or added before their reviews can sync.
  2. Mapped learners missing reviews - a linked learner whose Neon review count is
     below their Aptem count (e.g. new reviews added since the last sync).
  3. Started/Completed reviews without captured content - reviews that should hold
     content but are stored only as 'partial', grouped by the stored reason
     (Aptem 5xx upstream, changed manifest, etc.).

Usage:
    python manage.py audit_review_gaps            # human-readable report
    python manage.py audit_review_gaps --json     # machine-readable JSON
    python manage.py audit_review_gaps --report path.json   # also write JSON
"""
import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import connection

from aptem.services.review_api_sync import (
    login_session, api_get, live_inventory, canonical_mapping,
)

STARTED = ("Completed", "Archived", "In Progress", "AwaitingSignature")
# Aptem OData Status values that are expected to carry content.
STARTED_SOURCE = {"Completed", "InProgress", "AwaitingSignature", "Archived"}


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Command(BaseCommand):
    help = "Audit Aptem-vs-Neon review coverage and report the gaps."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit JSON only.")
        parser.add_argument("--report", default="", help="Write the JSON report to this path.")
        parser.add_argument("--limit-lists", type=int, default=50,
                            help="Max rows to print per section (JSON always holds all).")

    def handle(self, *args, **options):
        session = login_session()
        try:
            live = live_inventory(session)
        finally:
            session.close()
        mapping, _ = canonical_mapping()

        aptem_by_learner = defaultdict(list)
        for r in live:
            aptem_by_learner[str(r.get("LearnerId"))].append(r)

        with connection.cursor() as c:
            c.execute('SELECT learner_id, count(*) FROM "Learner".reviews GROUP BY learner_id')
            neon_count = {str(lid): n for lid, n in c.fetchall()}
            c.execute('SELECT aptem_review_id, extraction_status FROM "Learner".reviews')
            neon_status = {str(rid): est for rid, est in c.fetchall()}

        # 1. Unmapped learners
        unmapped = {aid: rows for aid, rows in aptem_by_learner.items() if aid not in mapping}
        names = {}
        if unmapped:
            session = login_session()
            try:
                for grp in _chunks(list(unmapped), 40):
                    flt = " or ".join("Id eq %s" % a for a in grp)
                    d = api_get(session, "/odata/1.0/Users",
                                params={"$filter": flt, "$select": "Id,Email,FullName"}).json()
                    for u in d.get("value", []):
                        names[str(u.get("Id"))] = (u.get("FullName"), (u.get("Email") or "").strip())
            finally:
                session.close()
        unmapped_list = [{
            "aptem_id": aid, "full_name": names.get(aid, ("?", "?"))[0],
            "email": names.get(aid, ("?", "?"))[1], "reviews": len(rows),
        } for aid, rows in sorted(unmapped.items(), key=lambda kv: -len(kv[1]))]

        # 2. Mapped learners with fewer reviews in Neon than Aptem
        gaps = []
        for aid, our_lid in mapping.items():
            aptem_n = len(aptem_by_learner.get(aid, []))
            if not aptem_n:
                continue
            neon_n = neon_count.get(str(our_lid), 0)
            if neon_n < aptem_n:
                gaps.append({"our_id": our_lid, "aptem_id": aid,
                             "aptem": aptem_n, "neon": neon_n, "missing": aptem_n - neon_n})
        gaps.sort(key=lambda g: -g["missing"])
        if gaps:
            with connection.cursor() as c:
                for g in gaps:
                    c.execute('SELECT full_name FROM "Learner".learners WHERE id=%s', [g["our_id"]])
                    row = c.fetchone()
                    g["full_name"] = row[0] if row else "?"

        # 3. Started/Completed reviews without captured content
        missing_content = [str(r["Id"]) for r in live
                           if str(r.get("LearnerId")) in mapping
                           and r.get("Status") in STARTED_SOURCE
                           and neon_status.get(str(r["Id"])) != "success"]
        reasons = Counter()
        if missing_content:
            with connection.cursor() as c:
                for grp in _chunks(missing_content, 500):
                    c.execute(
                        '''SELECT status, left(coalesce(last_error, '(none)'), 80), count(*)
                           FROM "Learner".reviews WHERE aptem_review_id = ANY(%s)
                           GROUP BY 1, 2''', [grp])
                    for status, err, n in c.fetchall():
                        reasons["%s | %s" % (status, err)] += n

        report = {
            "aptem_total_reviews": len(live),
            "aptem_learners": len(aptem_by_learner),
            "unmapped_learners": unmapped_list,
            "unmapped_reviews": sum(u["reviews"] for u in unmapped_list),
            "mapped_missing": gaps,
            "started_without_content": len(missing_content),
            "started_without_content_reasons": dict(reasons),
        }

        if options["report"]:
            with open(options["report"], "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        if options["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        self._print(report, options["limit_lists"])

    def _print(self, rep, limit):
        w = self.stdout.write
        w("=" * 68)
        w("REVIEW COVERAGE AUDIT")
        w("Aptem: %d reviews / %d learners" % (rep["aptem_total_reviews"], rep["aptem_learners"]))
        w("=" * 68)
        w("\n1) LEARNERS NOT PULLED (unmapped): %d learners, %d reviews"
          % (len(rep["unmapped_learners"]), rep["unmapped_reviews"]))
        for u in rep["unmapped_learners"][:limit]:
            w("   %-26s | %-38s | aptem_id=%s | %d reviews"
              % (u["full_name"], u["email"], u["aptem_id"], u["reviews"]))

        w("\n2) MAPPED LEARNERS MISSING REVIEWS (Neon < Aptem): %d" % len(rep["mapped_missing"]))
        for g in rep["mapped_missing"][:limit]:
            w("   %-26s | our_id=%s aptem_id=%s | Aptem %d vs Neon %d (missing %d)"
              % (g.get("full_name", "?"), g["our_id"], g["aptem_id"], g["aptem"], g["neon"], g["missing"]))

        w("\n3) STARTED/COMPLETED REVIEWS WITHOUT CONTENT: %d" % rep["started_without_content"])
        for reason, n in sorted(rep["started_without_content_reasons"].items(), key=lambda kv: -kv[1]):
            w("   %4d  %s" % (n, reason))

        w("\n" + "=" * 68)
        w("SUMMARY: unmapped_reviews=%d | mapped_missing_learners=%d | started_without_content=%d"
          % (rep["unmapped_reviews"], len(rep["mapped_missing"]), rep["started_without_content"]))
        w("=" * 68)
