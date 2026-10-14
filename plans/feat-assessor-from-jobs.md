# feat/assessor-from-jobs
- **Status:** releasing (fast path per Eli)
Assessor was engagement.created_by (a username string "admin"). Per Eli: it should be the display name(s)
of the SOURCE-JOB owners; multiple operators (internal+external) -> "Assessment Team".
- host.py: job_owners(job_ids)->{job_id:owner_id} + display_identity(user_id) wrappers (host_hook).
- context.py: _derive_assessor(engagement) — distinct source-job owners -> display names; 1->name, >1->
  "Assessment Team", none->None. Overlaid onto ctx.assessor (docx) AND variables["ASSESSOR"] (html masthead
  + {{ASSESSOR}}), so both renderers read the ONE value.
- Needs core seam extras["job_owners"] (feat/job-owners-seam); degrades to None (row omitted) unmounted.
Tests: 4 derivation cases + updated cover test. 31 report tests green.
