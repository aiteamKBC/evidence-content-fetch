# Aptem review reconciliation

`sync_all_aptem_reviews` reads Aptem's global OData review inventory and maps each
review to `"Learner".learners` by the unique `aptem_id`. A unique email fallback
is allowed only when the canonical learner has no conflicting Aptem identity; an
apply run persists that identity for future exact matches.

The Aptem review ID is the global upsert key. Migration `0008` enforces that key
in Neon so five workers cannot create duplicates. Non-started reviews store live
metadata. Content capture version 6 includes Archived reviews and re-fetches
older captures. Stable Completed/Archived captures may be reused only after
verified capture at this version and with an unchanged source UpdatedDate.
Section captures retain their original source JSON as well as rendered answers.
Zero, false, and duplicate field titles survive conversion. Dropdown options
(`listAnswers`) are definitions, not selected answers.

Unknown sections and failed requests are explicitly unverified. Prior answers
are retained for failed sections and the review remains partial. Only Aptem's
exact `There is no corresponding Review meeting` response proves an absent
meeting; its response and the Extended metadata are preserved separately.
An identical-type template can recover accessible content but cannot prove
the target's complete section membership, so reconstructed manifests remain
unverified. Skill Radar identifies its source as the current learner competency
report, not a historical review snapshot.

If that fallback lacks a type GUID, it can recover the GUID from Aptem's
documented `LightRead` type-list read query. Although this read endpoint uses
POST with an empty body, it does not edit or start reviews. The target's own
learner/programme identity and exact numeric type ID must match a unique GUID;
request and response evidence is retained. This can recover a Previous Meeting
Summary, but never establishes the target's missing section-manifest membership.

The `ReviewContent` section list is not the whole UI. When Meeting Details is
present, v6 also captures the saved Meeting Summary, transcript notes/file
metadata, and the default Previous Meeting Summary from the same-type previous
review. Original responses and that previous review ID are retained. Auxiliary
descriptors are separate from the original section list. Transcript file metadata
does not mean the binary files have been downloaded.

Nonempty prior sections missing from the current capture are retained explicitly
as unverified historical content, even if all base section requests succeeded.
Known RAG display aliases can be reconciled after a verified current RAG response.
Database section inserts are batched without weakening the atomic upsert.
Actions retain either the review snapshot or Aptem's current learner-programme
fallback, explicitly identified by `source_scope`. A current programme is not
silently substituted for a historical review programme in provenance metadata.

Run a read-only plan or an apply from `backend`:

```powershell
python manage.py sync_all_aptem_reviews --workers 5
python manage.py sync_all_aptem_reviews --apply --workers 5
```

Use `--review-ids 123,456` or `--limit N` for pilots. The main checkpoint is
`debug/all-aptem-reviews-sync.json`; its `.lock` sibling means the coordinator is
running. The PowerShell launcher starts the five-worker process hidden and writes
timestamped stdout/stderr logs:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_all_aptem_reviews_sync.ps1 -Workers 5
```

The final report distinguishes mapped and unresolved Aptem learners, metadata-only
updates, complete section captures, and per-review errors. Stored reviews absent
from the current Aptem inventory are reported during verification and are never
deleted automatically.

## Repair and readback verification

```powershell
python manage.py repair_review_content --apply --scope archived --workers 5 --report ..\debug\archived-content-repair.json
python manage.py repair_review_content --apply --scope content --workers 5 --report ..\debug\review-content-repair.json
```

`content` covers Completed, In Progress, Awaiting Signature, and Archived.
`all` also requests manifests for Scheduled/Not Scheduled reviews; these may
return source errors because they have not started. `--review-ids` narrows a
pilot. `--resume` skips verified results only if the saved content version and
source UpdatedDate still match. No external Aptem records are modified.

For a mixed, explicitly selected delta, use `--scope sync`. It captures content
for started statuses and updates metadata only for Scheduled/Not Scheduled.
Metadata readback must retain existing sections, capture evidence and extraction
timestamps exactly; it is reported as `metadata_verified`, not verified answers.
Both paths back up prior records and validate their save inside a transaction.
Missing selected source IDs or ambiguous learner/review identities stop the
preflight before any record writes.

An explicit sync selection is limited to 200 positive numeric review IDs and
uses a filtered Aptem inventory read. Its `live_total` is the selected source
inventory size, not the tenant-wide review total. Default full-inventory reads
are unchanged. Example:

```powershell
python manage.py repair_review_content --apply --scope sync --workers 5 --review-ids 123,456 --report ..\debug\review-delta-sync.json
```

Before each repair, prior Neon records and section answers are backed up under
`debug/review-content-before/<timestamp>/<aptem_review_id>.json.gz`. Keep these
private: they contain learner data. The command reads back each saved review
and compares identity, manifest, original response JSON, rendered fields, tables,
and text. Reports distinguish verified, unverified, and failed captures; neither
a matching review count nor `success` from an older importer proves completeness.

HTTP failure diagnostics retain the request method, query-free path, observed
status and attempt count. A failed authentication refresh must not hide the
original 401/403, and an exhausted 500 retains its source path. These diagnostics
contain no credentials, headers or response bodies. They explain a partial
capture; they do not prove absent answers or authorize source-record changes.

To append omitted nonempty sections from a specific backup without reverting
corrected answers, first plan, then apply. Existing rows are rechecked under lock;
the restore is idempotent and marks restored content unverified pending recapture.

```powershell
python manage.py restore_review_capture_backups --backup-directory ..\debug\review-content-before\RUN_TIMESTAMP
python manage.py restore_review_capture_backups --apply --workers 5 --backup-directory ..\debug\review-content-before\RUN_TIMESTAMP
python manage.py audit_review_content_capture --report ..\debug\review-content-independent-audit.json
```

The independent audit fetches the current live inventory and reads one consistent
read-only Neon snapshot. It checks identities, source freshness, base and
supplemental coverage, stored section consistency, and actual declared answers.

For a targeted post-sync check, `audit_review_content_capture --review-ids 123,456`
checks the same explicit selection in both the live source and stored snapshot.
The report labels that scope and lists the selected IDs; `complete: true` in a
selected report never means the entire tenant is complete. Missing selected
database rows and ambiguous canonical mappings remain failures.

`reconcile_preserved_review_sections` can reclassify proven v6 duplicates from
their saved source payloads without refetching answers. It requires unchanged
source identity/status/UpdatedDate, backs up each affected review, keeps its
original extraction timestamp, and leaves unknown cases partial. Its apply mode
shares the repair lock and must run after the main repair exits.

The duplicate proofs are intentionally narrow: schema-only empty controls,
the exact Reviews Schedule table/pager, the literal empty Learning Progress
placeholder, and the complete collapsed-history Skills Radar UI projection.
Skills Radar requires the exact unchecked Show History control, known role
labels, statuses and axis labels; extra or unexplained content stays preserved.
An old blank actual review date may match a newly populated source date, but
changed nonempty dates or changes to other table cells do not prove duplication.
These checks reconcile display aliases; they do not manufacture missing answers.

```powershell
python manage.py reconcile_preserved_review_sections --review-ids 17674,16279
python manage.py reconcile_preserved_review_sections --apply --workers 5
```

`scripts/finish_review_content_repair.ps1 -RepairProcessId PID` is a scoped
completion helper for `debug/review-content-v6-repair.json`. It verifies the
process identity, waits for normal repair completion, runs preserved-section
reconciliation, refetches timestamp-unproven candidates once, then runs the
independent live audit. Unexpected source failures also receive one bounded
retry; known Archived manifest failures and the reconstructed review remain
explicit rather than entering a retry loop. It never restarts a killed or
failed repair. To stop all review work, stop both the repair and this helper;
their process IDs are reported when launched. A finished helper does not imply
that source errors or unverified content were resolved: read the audit results.

The completion helper also uses the independent audit to identify and fully
recapture reviews with the obsolete PLR-to-PriorLearning mapping, then repeats
the audit. PLR is a separate learner-scoped source, not an alias of Prior
Learning. The final independent audit, not the main pass's historical counts,
is the authoritative completion report.
