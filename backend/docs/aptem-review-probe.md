# Aptem read-only navigation probe

This is a headed development probe for Betty Heaton, not a batch extractor.
It uses Django settings and the existing `login_to_aptem` helper. Credentials
remain in the existing environment; it does not rewrite `.env`, store cookies,
connect to the database, or create/edit reviews. MFA is not bypassed.

## Run

From `backend` in PowerShell, using the local installation created for this mapping:

```powershell
$env:PYTHONPATH = (Resolve-Path '..\.playwright-runtime').Path
python manage.py probe_aptem_reviews
```

For a normal Python environment, install `backend/requirements.txt` first and
run the same Django command without the `PYTHONPATH` assignment. The probe uses
installed Microsoft Edge (`channel="msedge"`) in headed mode. It needs network
and desktop access. It waits up to 30 seconds for elements and 60 for navigation.
Browser and context cleanup runs on success and failure.

## Live mapping

All paths below are relative to the configured `APTEM_BASE_URL`.

| Step | Observed path | Confirmed selector |
| --- | --- | --- |
| Authenticated start | `/pwa/caseload-dashboard` | `get_by_role("link", name="Learners", exact=True)` |
| Learners | `/pwa/learners` | `get_by_role("searchbox")`; stable input ID `search` |
| Ownership filter | same | `#learnerOwnershipQuickFilter`, then role `option`, exact name `All Learners` |
| Betty row | same | role `row` containing exact text `Betty Heaton`; Programme Status header maps to its `aria-colindex` cell |
| Learner dashboard | `/pwa/learners/15997/dashboard` | Within verified Active row: role `option`, exact name `Dashboard` |
| Reviews | `/pwa/learners/15997/reviews/upcoming` | role `link`, exact name `Reviews` (href ends in `/reviews`) |
| Finished | `/pwa/learners/15997/reviews/finished` | role `button`, exact name `Finished`; also observed `auto-id="finished-btn"` |
| First View | `/pwa/learners/15997/reviews/review-content/17732` | First Completed row containing role `button`, exact name `View` |
| Sections | same | `div[role="button"][aria-expanded]`; alternatively role/name intersected with `[aria-expanded]` |

Betty's live row and dashboard both showed Active. The learner ID is 15997.
The probe checks this known development identity after the name search.
The first review observed was `Personal Support Plan - 18/06/2026`, ID 17732.
The Finished table showed three Completed reviews, one page, next-page disabled.
The other two were monthly coaching reviews. Only the first View was opened.

The Personal Support Plan exposes six expandable sections:

- Learner Information
- Functional Skills
- Induction Checklist
- Additional Support
- Careers Education, Information Advice and Guidance (CEIAG) & Goal Setting
- Reviews Schedule

All six could be expanded together. The probe records section headings, not
structured answers. Screenshots naturally contain the displayed review content.

## Navigation findings and limitations

- The app uses Angular SPA navigation. A click can return while URL, title and
  old page content still reflect the previous route. Wait for the final path
  and destination elements. Reviews redirects from `/reviews` to `/reviews/upcoming`.
- Observed titles: `Dashboard - Kent Business College`, `Learners - Kent Business
  College`, `Learner Dashboard - Kent Business College`, and `Learner Reviews -
  Kent Business College`. The title alone does not distinguish list/detail.
- No iframe was observed on the mapped screens; the review is in the main page.
- Learner navigation hrefs expose the learner ID. The list Dashboard action is
  a Kendo split-button, not a learner hyperlink.
- Review View controls are buttons without hrefs. The detail URL exposes the
  review ID after navigation; no pre-navigation review ID is assumed.
- Kendo pager buttons expose `Go to the next page`, `Go to the previous page`,
  `Go to the first page`, `Go to the last page`, and `Page 1`; the page-size
  combobox is labelled `items per page`. Planned showed 15 items across two
  pages. Finished had only one page, so cross-page navigation remains untested.
- Search initially applies to My Learners. The probe switches to All Learners,
  then searches; it does not open unrelated learners.
- The placeholder matches a custom wrapper and its input. Use role searchbox.
- Section names match nested buttons twice. Target the aria-expanded control.
- Generated Angular/Kendo classes and UUIDs are not stable selectors.
- Other review types, empty results, multi-programme ambiguity and session expiry
  still need broader testing before any batch extractor. Unconfirmed Active status
  or identity stops this probe. No full extraction or database storage is implemented.

## Diagnostics

`debug/navigation.json` records step, URL, title, selector strategy, result,
iframe count, and selected navigation metadata. `debug/01-learners.png` through
`debug/07-expanded-sections.png` capture the route. Errors return a nonzero
command exit and, where still authenticated, `debug/failure.png`.
These files are local, ignored by Git and overwritten by subsequent runs.
They contain learner information and should not be published.

## Store the mapped first review

From the same configured environment:

```powershell
python manage.py import_betty_review --dry-run
python manage.py import_betty_review
python manage.py import_betty_review --all-finished
```

This separate command selects Active Aptem learner 15997 from
`"LMS"."Aptem_users"` and requires a unique email match in `"Learner".learners`.
The confirmed destination learner is 398. It then runs the live Active checks,
captures the first Completed review and all expanded sections, and stores one
review in `"Learner".reviews` and its sections in `"Learner".review_sections`.
By default it processes the first review only. `--all-finished` enumerates all
Finished pages, rechecks live dashboard Active status before opening each review,
verifies the row metadata, and saves every Completed review for Betty. It does
not process other learners. Combine with `--dry-run` for extraction without writes.
The all-review payload is saved to `debug/betty-reviews.json`.

`debug/betty-review.json` contains the captured payload. Section text, HTML,
custom question/answer fields, learner information fields and tables are retained.
Original date strings remain in `review_data.source_metadata`: the typed timestamp
columns are left unset because Aptem's displayed timezone has not been verified
and the completion value has date-only precision.

The import transaction serializes review writes and replaces sections only for
the same destination learner/Aptem review ID; duplicate existing parents cause
an error. It does not alter either table's schema. Dry-run never calls persistence.

## Planned reviews

`python manage.py import_betty_planned_reviews` reads Planned list metadata across
pages. Add `--dry-run` to avoid writes. It reads review IDs from existing schedule
hrefs but never follows these actions, starts reviews or edits Aptem data.
Planned records retain their actual status and are marked `partial` to indicate
list-only capture; no artificial section records are created. Finished records
are never downgraded to Planned metadata. Run `import_betty_review --all-finished`
for Finished review contents. Both commands default to headless execution through
`APTEM_BROWSER_HEADLESS=true`. Gateway 403 responses must be resolved before
live extraction can proceed; the command does not bypass access controls.
