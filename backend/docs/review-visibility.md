# Aptem review membership

The local six-hour worker records every review ID displayed across all Classic
programme grids and pages. `reviews.review_data.aptem_visibility` stores that
verified membership independently of content capture and signature status.

Consumers listing current imported reviews must require both `verified=true`
and `visible=true`. Join `review_sections` through those selected review IDs.
Use `scripts/current_aptem_reviews.sql` for both tables and
`scripts/review_signature_queries.sql` for missing-signature follow-ups.
Membership without a marker is unknown, not current. `checked_at` is the time of
the browser inventory; failures retain the last verified membership and must
not be described as a new successful source check.

The physical tables intentionally retain historical rows, existing primary keys,
signatures, and dependent coach/case records. Direct unfiltered table counts do
not represent the current Aptem list. Do not use `prune_hidden` to reconcile them.
Content fetch errors do not make a displayed review disappear. A source review
missing entirely from storage prevents membership reconciliation for its learner.

`refresh_visible_reviews` reconciles membership each successful inventory/import.
To backfill from the newest checksum-verified browser inventory per learner:

```powershell
python manage.py reconcile_review_visibility
python manage.py reconcile_review_visibility --apply
```

The default is read-only. Up to four database workers process batches of ten
learners, with short transactions and exact membership readback. Active worker
learner locks are respected; busy or
failed learners are reported and give a nonzero exit code. Re-run for those
learners using `--learner-id`. Recovery files contain before/after values of the
metadata key, are flushed and checksum-verified before writes, and are never
overwritten. `restore_visibility(path)` performs a transactional compare-and-set
rollback of that key only, refusing to undo newer membership evidence. No schema
migration or deployment is needed for the importer; consumer code changes must
be released separately under the consuming project's release rules.
