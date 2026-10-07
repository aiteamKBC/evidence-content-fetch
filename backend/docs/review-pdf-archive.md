# Original review PDF archive

`sync_review_pdfs` copies the original Aptem PDFs to private Azure Blob Storage.
It does not generate PDFs from scraped text or change source signatures. Signature
labels are recorded only as an observation, never as proof that all parties signed.

## Scope and identity

The initial inventory is `fetching_evidence.evidence_items`, restricted to file
names matching `Review - %.pdf`. This includes Progress Review, Monthly Coaching,
Eligibility, Support Plan and other review types. It is not a live enumeration of
every document in Aptem. Separately named/manual uploads need separate discovery.

`evidence_items.learner_id` is the **Aptem ID**, not `Learner.learners.id`.
The importer uses a unique `learners.aptem_id`, or unambiguous email mapping when
that field is absent. Unresolved identities remain null and are not exposed by the
download endpoint. A PDF is linked to an existing review only when the learner,
PDF title/type, completion date, and available planned date select one review.
Absent or ambiguous review matches remain explicitly unlinked; no reviews are invented.

## Storage and recovery

Apply migrations through `aptem.0007_review_pdf_sync_lease` once. `0006` adds
`"Learner".review_documents` with nullable learner/review foreign keys, a unique
source evidence ID, upload status, attempts, retry times, and archive metadata.

Original blobs use
`original-review-pdfs/learner-{aptem_id}/evidence-{evidence_id}/{sha256}.pdf`.
Uploads never overwrite an existing blob. A full read-back SHA-256 check is
required before the database status becomes `uploaded`. Failed database commits
can be retried using the same deterministic blob name. Only account/container/blob
names are persisted, never Azure SAS credentials. PDFs download into bounded
temporary storage and are discarded locally after transfer.

The five threads process separate learners and process each learner's documents
sequentially. A renewable database lease prevents a second coordinator, with owner
checks on every queue write. Migration `0007` adds this lease table. Unlike a
session advisory lock, this works through PgBouncer transaction pooling. Lease
heartbeats run with checkpoints; a crashed process's lease expires after ten
minutes, and an expired owner cannot update documents or renew another run's lease.
Restarting
the command resumes pending/retry/interrupted items and skips uploaded files.
Authentication refresh is serialized and held only in memory. Transient failures
use backoff. Temporary Azure/Aptem service outages pause workers for a five-minute
cooldown and remain retryable even after five attempts; Azure permission failures
still stop the coordinator. Startup storage checks also retry temporary network
outages and respect the stop file/deadline. With `--run-hours 0`, known temporary
per-file network interruptions remain retryable without an attempt limit, too.
Invalid PDFs and other permanent failures remain explicitly failed, never marked
uploaded or retried endlessly; unknown errors retain the configured attempt cap.
A new run discovers newly added
evidence IDs; changes to an already uploaded evidence ID are not refreshed by
default, so a later version-refresh policy is still needed.

## Commands (from backend)

```powershell
python manage.py sync_review_pdfs --scope all
python manage.py probe_review_pdfs --evidence-ids 38367,21134,41162
python manage.py sync_review_pdfs --apply --scope all --workers 5 --limit 5
python manage.py sync_review_pdfs --apply --scope all --workers 5 --run-hours 8
python manage.py sync_review_pdfs --apply --scope all --workers 5 --run-hours 0
python manage.py sync_review_pdfs --status
```

Without `--apply`, the sync command only reports inventory. `--scope active`
restricts source Aptem IDs to database Active users. `--learner-ids` accepts Aptem
IDs; `--evidence-ids` selects a pilot. Probe PDFs and checksums are saved in
`debug/review-pdf-pilot/`; these are sensitive local artifacts, not web media.

The runtime checkpoint is `debug/review-pdf-sync.json`. Creating
`debug/review-pdf-sync.stop` requests a graceful stop after in-flight requests.
The stop file must be removed deliberately before starting another run. No old
audit watchdog, browser workers, Zoom sync or Django server is started by this job.
`--run-hours 0` removes the runtime deadline; the process exits once every queued
file is uploaded, or reports `finished_with_errors` for permanent file failures.
This is a background process, not a Windows service: the computer must remain on
and connected, and closing/killing Python or restarting Windows interrupts it.

## LMS download integration

Both routes require a real authenticated **staff** session; local auth bypass is
not accepted for document access:

- `GET /api/evidence-tool/learners/{internal_learner_id}/review-documents/`
- `GET /api/evidence-tool/review-documents/{document_id}/file/`

The download is streamed back as a PDF after size/checksum verification. There are
no public blob URLs or SAS tokens in the API response. Document list entries have
their nullable `review_id`, document type, filename, and linkage status. The LMS
frontend must call these endpoints and use its authenticated staff session. If
student/coach-specific access is required, add the LMS's own object permissions
before widening access beyond staff; this repo does not contain that frontend.

`AZURE_REVIEWS_CONTAINER_URL` supplies the existing private `reviews` container
credential. A container **service SAS** cannot call Get Container Properties;
that 403 is not a valid test of blob read/write permission. The preflight uploads
and reads a harmless `_archive-health/review-pdf-sync-v1.txt` marker and confirms
that an anonymous request cannot read that existing blob. No sensitive PDF is
uploaded if that privacy check fails. The marker is retained for subsequent runs.
The importer does not change Azure account firewalls or container permissions.
