# EDD: feat/qualify-noncve (ext#223)

CVE-less exploitability qualifier — a walled-off sibling to `assemble_verdict`.

## What ships

- `exploiteer/qualify.py` — `ExploitabilitySignal` dataclass + `qualify_noncve(finding, context)`
  - CPU-only LAYA singleton via `@cache`
  - Degrade-to-`None` on any exception (mirrors vulnx pattern in `feeds.py`)
  - Guard: `if finding.cve: return None` — CVE-keyed findings never enter this path
- `pyproject.toml` — `laya` optional extra (`uv sync --extra laya`)
- `lotek-extension.toml` — `qualify_noncve_enabled` bool setting (default `false`), `Qualifier` group
- `exploiteer/orchestration.py` — call `qualify_noncve` for CVE-less findings when enabled; attach
  signals to `Analysis.noncve_signals`
- `exploiteer/templates/exploiteer/plan.html` — new "CVE-less estimated exploitability" section,
  visually distinct (no KEV/MSF badge style, explicit "calibrated estimate" label)
- `tests/test_qualify_armgate.py` — arm-gate invariant: asserts no code path lets an
  `ExploitabilitySignal` influence `should_execute` or appear as a `Candidate` for execution

## Graders (acceptance / EDD)

### Regression — wall holds (must pass before merge)

`test_qualify_armgate.py::test_signal_never_reaches_execution`

- Calls `qualify_noncve` with a synthetic CVE-less finding → `ExploitabilitySignal`
- Confirms `execution.should_execute` and `execution.build_execution_plan` accept no path that takes
  an `ExploitabilitySignal` as input (type-level + runtime assertion)
- Also confirms `qualify_noncve(cve_finding, ctx)` returns `None` when `finding.cve` is set

### Isolation — CVE-keyed paths unchanged

`test_qualify_armgate.py::test_cve_finding_skipped`

- A finding with a CVE → `qualify_noncve` returns `None`, no signal emitted

### Capability (offline, deterministic)

Deferred to post-fine-tune: a labeled holdout of CVE-less findings measures precision/recall on
surfaced paths. Pinned checkpoint SHA lands in `provenance`.

### Calibration (offline, deterministic)

Deferred to post-fine-tune: ECE on the holdout.

## Non-goals (enforced by the wall)

- No model output sets or influences `Verdict.tier` or any `should_execute` predicate
- No new hard dependency on `laya`/`torch` — optional extra only
- `qualify_noncve` never imported by `verdict.py` or `execution.py`
