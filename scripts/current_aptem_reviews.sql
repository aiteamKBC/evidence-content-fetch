-- Current reviews: exactly the IDs in the last verified Classic inventory for
-- each learner. Keep checked_at in reports: this is a snapshot, not a live query.
-- Unknown membership is excluded. An Aptem timeout must not hide prior records.
-- The underlying tables retain historical rows and all dependent references.
SELECT r.*
FROM "Learner".reviews r
WHERE r.review_data->'aptem_visibility'->>'verified'='true'
  AND r.review_data->'aptem_visibility'->>'visible'='true';

-- The same membership applies to the second table through its parent review.
SELECT s.*
FROM "Learner".review_sections s
JOIN "Learner".reviews r ON r.id=s.review_id
WHERE r.review_data->'aptem_visibility'->>'verified'='true'
  AND r.review_data->'aptem_visibility'->>'visible'='true';
