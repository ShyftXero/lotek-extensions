# feat/scribble-reproduction-block

- **Status:** in review — code + tests green; PR to follow.

## Purpose

Findings had no structured **reproduction** field, so repro steps were buried in free prose. Add a
first-class, copy-pastable `reproduction` content block (step 1/2/3 or a command block), omitted from
the rendered report when empty. It doubles as the render surface for exploiteer's candidate
exploit-chain reproductions (lotek plan `steady-mixing-owl` / #431 follow-on). **This block is the
output surface — the value is the chaining that fills it, which lands in exploiteer, not here.**

## Done

- **First-class block:** `reproduction` added to `content/schema.py DEFAULT_BLOCKS` and to
  `reporting/render_html.py _BLOCK_ORDER` (after `details`) + `_BLOCK_LABELS` ("Reproduction"). Rides
  the existing omit-when-empty path (`_render_blocks`' `if fragment:`), so no renderer special-case and
  no report change when absent. `layouts.BLOCK_KEYS` is the report-SECTION set, not per-finding blocks —
  deliberately untouched.
- **Copy-pastable:** authored as a ProseMirror `codeBlock` (`schema.code_block_doc`, verbatim, newlines
  preserved) → renders `<pre><code>` (both tags already in the sanitizer allow-list).
- **Authoring:** `reproduction` added to `api_pat._PATCH_CONTENT_FIELDS`; a plain-text convenience arm in
  `_author_content_json` wraps it as a code block. Covers create AND patch (patch routes through the
  same helper).
- **Deterministic auto-fill on promotion:** `facts.reproduction_from_facts` (PURE, tool-agnostic — keyed
  on fact SHAPE per CONTRACT-FACTS §7.3, not a tool name) turns a scan finding's own PoC facts
  (`curl` [+ `extracted`], or an injected `method`/`url`/`param`/`payload` request) into repro text.
  `EngagementFinding.from_lotek_finding` (the single home for raw-bridged promotions) fills the block
  from it, after the `details` fallback so it never suppresses it. No PoC → no block → omitted.

## Evals / tests

- `tests/test_facts_shapes.py` — `reproduction_from_facts` (curl verbatim; +extracted; injected request;
  no-PoC → ""; non-dict → ""). Stays tool-agnostic (its denylist test).
- `tests/test_reproduction_block.py` — `code_block_doc`; renders `<pre><code>` copy-pastable; first-class
  position after `details`; `from_lotek_finding` fills the block from a curl PoC and omits it without;
  auto-fill doesn't clobber the details fallback; authoring convenience field + PATCH allow-list.
- Red-then-green verified (auto-fill neutralized → the promotion test FAILS; restored → PASSES). ruff
  clean; my new code (`facts.py`/`schema.py`) pyrefly-clean.

## Remaining

- Ext PR → merge (auto release-tag) → re-pin scribble `tag=` in lotek + `uv lock` + mounted tests.

## Notes

- Pre-existing pyrefly noise in `api_pat.py`/`models.py`/`render_html.py` (scribble ships no
  `[tool.pyrefly]` config) is unrelated to this change; my additions type-check clean in isolation.
- The auto-fill covers the `from_lotek_finding` (raw-bridge) path; template-matched promotions carry the
  library template's content (a template can author its own reproduction block). Per-child repro on
  template-matched bulk promotes is a later nicety.
