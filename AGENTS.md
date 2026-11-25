# lotek-extensions repo rails

This file holds repo-specific rails. Cross-repo content lives in
[lotek-cadre/AGENTS.md](https://github.com/ShyftXero/lotek-cadre/blob/main/AGENTS.md).
Read that file first. Do not copy it here.

## Repo rails

**Tests**

- Each extension is its own subproject. Run its tests in that subproject: `cd cream && uv run --extra dev pytest -q`.
- Add a mounted test in lotek core when a behavior must work inside lotek: `lotek/tests/test_<ext>_extension.py`.
- Check style with `uvx ruff check <ext>` and `cd <ext> && uv run --extra dev pyrefly check`.
- Read `README.md` for the extension list and the local dev flow.

**Release and pins**

- Merge the change to `main`. `.github/workflows/release-tag.yml` cuts a dated tag `v<YYYY.M.D.HHMMSS+g<sha>>`. Do not cut the tag by hand.
- lotek installs each extension by tag. A tag here is not a deploy. lotek deploys the extension only after it re-pins the tag and cuts a core release.

**Tool quirks**

- An extension reaches lotek only through the injected host contract. Do not import lotek or another extension.
- `kit/` is a shared contract library, not an extension. It has no `lotek-extension.toml` and no `register()`.
- Keep a core reference column as `SoftHostId`, never as `Integer` or `String`. Postgres refuses the cast.
- Do not install an extension as editable in lotek. An editable install skips the wheel build, so discovery skips the extension.
- Stage explicit paths. Do not use `git add -A`.

## Lanes

Route work by the roster in the global file. Do not restate the roster table.

- Extension seam and the host contract: `acid-burn`.
- Tests: `cereal-killer`.
- Security review of an extension: `margo`.

## Notes

- `AGENTS.md` is the only instruction source. Do not add a `CLAUDE.md`.
