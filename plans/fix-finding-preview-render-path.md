# fix/finding-preview-render-path
- **Status:** releasing (fast path per Eli)
Finding /preview (resolve_finding) built context with NO overlay -> {{AFFECTED}}/{{TARGET_URL}}/{{DOMAIN}}/
custom vars survived verbatim, and it resolved the verbatim reproduction block. Drifted from the report.
Fix: resolve_finding is now the ONE home — applies finding_overlay_variables (promote overlay + aggregation-
parent count-ref) + renders reproduction RAW + skips suppressed. reporting.context._finding_ctx calls it.
Tests: preview resolves overlay + reproduction stays raw; report suites unchanged (behavior preserved).
