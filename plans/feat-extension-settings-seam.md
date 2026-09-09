# Plan: feat/extension-settings-seam

- **Branch:** `feat/extension-settings-seam` (off `main`)
- **Paired core branch:** `feat/ext-settings-and-attackpaths` in `ShyftXero/lotek` — **that** branch
  carries the host mechanism; this one is the consumer side and cannot be verified without it.
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Every extension should expose its configuration. Before this branch only **vector** appeared on
lotek's `/settings/extensions`, and not because the others had nothing — cream, scribble and exploiteer
each already had a full per-install config page that was simply never linked from the place an operator
looks for it (exploiteer's `/exploiteer/settings` was not even in its own `[[nav]]`).

Eli named two classes, and they need different treatment:

- **admin / per-install** — data sources and global config. Either declared as `[[settings]]` (the host
  owns the form) or, when the extension already owns a purpose-built page, pointed at with the new
  `[host] config_path` key so the host renders a link to it.
- **user / per-user** — preferences, *"things like default themes or color choices"*, which
  **must not require admin access**. That is the new `[[user_settings]]` table; scribble is its first
  consumer.

## Evals

- **Hypothesis:** each extension either declares a knob the host can render, links a page it already
  owns, or has genuinely nothing to configure — and every declared knob is actually READ by code, not
  merely rendered.
- **Mode / aggression:** 2
- **Capability evals** (must newly pass):
  - [x] bugreport `share_ttl_days` reaches `share_attachment(..., ttl_days=)` from both the browser and
        the PAT surface — 4 call sites, `uv run --extra dev pytest -q` green (95 passed)
  - [x] bugreport `max_attachment_mb` reaches `_CappedHeadReader`'s limit from both surfaces — same run
  - [x] exploiteer's five feed knobs resolve through `deps.feed_config_base()` with env winning —
        193 passed
  - [x] scribble `preferred_report_theme` is consulted by `_selected_theme` between `?theme=` and the
        install default — suite exit 0
  - [ ] the three `config_path` links render on lotek's admin page — proven on the CORE branch
        (`tests/test_extension_manifest_config_path.py`), since a link is a host behaviour
  - [ ] MOUNTED scribble theme-precedence test in `lotek/tests/test_scribble_extension.py`
- **Regression evals:** bugreport 95 passed · exploiteer 193 passed · scribble exit 0 (count line not
  captured — re-capture before the PR) · cream + registrar untouched (manifest-only / no change)
- **Graders:** each subproject's own suite; the mounted guards on the core branch.
- **Verdict:** pending the mounted tests + reviews.

## Done

- [x] **cream** — `[host] config_path = "/brand"`. Manifest only; `Brand` carries a logo data-URI, a
      Decimal tax percentage and multi-line terms, none of which survive the host's flat
      `ext:<name>:<key>` string seam, so the page stays here and the host links it.
- [x] **scribble** — `[host] config_path = "/themes"`, plus `[[user_settings]]
      preferred_report_theme`, `deps.host_user_setting`, and `_selected_theme` extended to
      `?theme=` → user preference → install default.
- [x] **exploiteer** — `[host] config_path = "/settings"` (the feed-source page, previously
      URL-only), plus five `[[settings]]`: `feeds_enabled`, `vulnx_enabled`, `feed_ttl_hours`,
      `feed_timeout_seconds`, `vulnx_cooldown_seconds`. `deps.host_setting` +
      `deps.feed_config_base()`.
- [x] **bugreport** — two `[[settings]]`: `share_ttl_days`, `max_attachment_mb`. `deps.host_setting`
      + clamped resolvers; the bounds moved to `models.py` beside `MAX_ATTACHMENT_BYTES`.

## Remaining

- [ ] MOUNTED tests on the core branch (a stub host proves the logic, never the mount).
- [ ] `/security-review` + `/adversarial-reviewer` on `git diff main...HEAD`.
- [ ] Per-extension suites re-run at final HEAD + `--ack-tests`.
- [ ] Merge here first so CI cuts a tag, then re-pin four `tag =` lines in lotek's `pyproject.toml`.
- [ ] Each touched extension's `docs/<NAME>.md` should mention its new knobs.

## Notes / gotchas

- **`service.py` in bugreport is deliberately Flask-free** (its docstring says so, and `host_audit` is
  already a passed-in parameter rather than an import). The settings values are therefore passed IN as
  keyword arguments defaulting to the module constants — so standalone and every existing unit test are
  unchanged — and only `blueprint.py`/`api_pat.py` call `deps`. Do not "simplify" this by importing
  `deps` into `service`.
- **Both clamp locally, on purpose.** The host validates against the manifest's `min`/`max`, and each
  resolver re-clamps anyway: these size a **security** bound (an upload ceiling; the lifetime of an
  unauthenticated capability URL; a rate-limit backoff whose minimum of 61s exists because the vulnx
  quota is a per-MINUTE window). A stale row written before the bounds tightened, a hook returning a
  string, or a `bool` — which is an `int` in Python and would silently mean 0 or 1 — must degrade to the
  shipped default, never to "no limit".
- **exploiteer precedence is env → saved setting → default**, and the direction matters:
  `EXPLOITEER_FEEDS=0` is how an air-gapped deployment declares it has no network, so a web form that
  could override a value pinned into the deployment would make that declaration advisory.
- **exploiteer feed URLs are NOT settings.** `resolve_feed_config` derives the runtime egress
  allow-list (INV-EGRESS-03) from the configured sources, so a URL field is an egress destination.
  Those stay with the deployment and with the audited `exploiteer_feed_sources` CRUD.
- **registrar gets nothing, deliberately.** `drivers.py` registers one driver per axis
  (`_COMPUTE`/`_DNS`/`_SMS`, all `{"null": …}`) and nothing else is tunable — a `choice` with a single
  choice is a decorative form for an unimplemented provider layer.
- **scribble keeps `ScribbleSettings`.** Its docstring offers to migrate `default_report_theme` to the
  host seam once `deps` grows the accessor. It now has `host_user_setting`, but the INSTALL default is
  cross-validated against `scribble_theme_overrides` rows and the host's generic form cannot do that —
  moving it would trade a validated picker for free text. The per-USER preference is what landed here.
- Local pytest on the dev box needs `TMPDIR` set; `/tmp/pytest-of-shyft` is owned by another uid and
  produces mass ERRORs that read as a code failure.
