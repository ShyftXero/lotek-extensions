# fix/scribble-idempotency-key-text

- **Status:** in review — PR open, awaiting human merge.

## Purpose
Fix the blocker behind lotek#411: every evidence upload to a UUID-era engagement 500s on Postgres with
`StringDataRightTruncation: value too long for character varying(80)`. `scribble_artifacts.idempotency_key`
is `VARCHAR(80)`; the client dedup key is `ev-<engagement>-<finding>-<basename>-<sha[:32]>`, and once #372
moved engagement/finding PKs to 36-char UUIDv7 a routine key runs ~120 chars. #372 widened the ids but not
this dependent column. SQLite never enforced the length, so the unit suite stayed green while prod 500'd.

## Done
- **Model:** `scribble/models.py` — `Artifact.idempotency_key` `String(80)` → `Text`. The basename alone is
  `String(512)`, so no bounded width is safe.
- **Migration:** new Alembic revision `d7b3f1a4c680` (off head `c2f8a1d3e460`) widens the column in place.
  Idempotent (guards on the reflected length), portable via `batch_alter_table` (plain `ALTER … TYPE TEXT`
  on Postgres — rebuilds the index, so `index=True` survives; table rebuild on SQLite). A fresh DB never
  runs it (built at TEXT from the model + stamped head).
- **Tests:** `tests/test_artifact_idempotency_key_width.py` — a hermetic model-contract guard (column must
  be unbounded `Text`; red→green verified) and a PG-gated migration test that replays the real prod path
  (stamp back to the prior head → narrow → `run_migrations` → widened; a >80-char key is refused before and
  stored after). The PG test was run for real against a throwaway DB on `lotek-test-pg-tmpl`: **2 passed**.
- ruff + pyrefly clean.

## Remaining
- Land here (squash-merge) → CI cuts a release tag.
- **Re-pin in lotek** (`[tool.uv.sources]` scribble `tag`) + `uv lock --upgrade-package scribble`, then the
  lotek-side helper fix (lotek#411 part 1) greens the `attach-evidence` acceptance tests end-to-end.

## Notes
- No Alembic-vs-hook choice: since lotek#335 scribble owns its schema through Alembic (`scribble_alembic_version`),
  so this is a proper revision, not a raw ALTER in `create_all`'s retrofit hooks.
- Orthogonal pre-existing main-red found while testing: `test_machine_artifacts.py::test_upload_emits_audit_row`
  + `test_update_emits_audit_with_transition` fail on origin/main with `UUID(...) == '01a…'` (a #372/#405-class
  stale string-vs-UUID comparison in the audit assertions). NOT touched here — worth its own issue.
