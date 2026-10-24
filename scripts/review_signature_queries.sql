-- Read-only queries for review/ticket consumers. No migrations are required.
-- signature_status is an independent, point-in-time snapshot. checked_at is its
-- freshness timestamp; reviews.status and last_extracted_at describe the content
-- import and are deliberately not overwritten by this signatures-only job.
-- Only verified state='missing' should produce missing-signature follow-ups.
-- Follow-up contacts are current assignments, not proof of who actually signed.
-- document_not_generated means a checked inventory contains no document; it
-- does not mean a learner failed to sign. no_signature_fields describes an
-- existing historical PDF with no signature controls and unavailable template
-- requirements; role.required remains null for that case.
-- PDF-derived signed_at values have date precision, recorded separately in
-- roles.<role>.signed_at_precision. Keep checked_at visible when making tickets.

SELECT r.aptem_review_id, r.learner_id, r.learner_name, r.review_type,
       s->>'checked_at' AS checked_at,
       role.key AS missing_role,
       role.value->'follow_up_contact'->>'name' AS contact_name,
       role.value->'follow_up_contact'->>'aptem_id' AS contact_aptem_id,
       role.value->'follow_up_contact'->>'basis' AS contact_basis,
       r.source_url
FROM "Learner".reviews r
CROSS JOIN LATERAL (SELECT r.review_data->'signature_status' AS s) snapshot
CROSS JOIN LATERAL jsonb_each(s->'roles') role
WHERE s->>'state'='missing' AND s->>'verified'='true'
  AND r.review_data->'aptem_visibility'->>'verified'='true'
  AND r.review_data->'aptem_visibility'->>'visible'='true'
  AND role.value->>'status'='missing'
ORDER BY r.learner_name, r.aptem_review_id, role.key;

-- The existing sections remain joined through review_id. Do not duplicate the
-- same signature snapshot into every content section.
SELECT r.id, r.aptem_review_id, r.review_data->'signature_status' AS signatures,
       rs.section_name, rs.fields_json, rs.tables_json
FROM "Learner".reviews r
JOIN "Learner".review_sections rs ON rs.review_id=r.id
WHERE r.aptem_review_id='9165'
  AND r.review_data->'aptem_visibility'->>'verified'='true'
  AND r.review_data->'aptem_visibility'->>'visible'='true'
ORDER BY rs.section_order, rs.id;
