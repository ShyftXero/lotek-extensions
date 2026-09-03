# lotek-kit

**This is not an extension.** It is a shared contract library that lotek core and lotek extensions both
depend on, so that neither has to depend on the other.

## Why it exists

The platform's rule is that an extension reaches lotek only through the injected host contract, never by
importing it. Core reaching the other way is worse — and core does exactly that today
(`lotek:src/app/routes/jobs.py` imports `vector.models` and writes an extension's table). Two consumers
needing the same code had nowhere neutral to put it, so it got copied instead: the repo currently carries
**three** hand-written native-HTML5 drag-reorder implementations, and the `attackpath/v1` document schema
is known by core as a hardcoded string literal.

`lotek-kit` is that neutral ground. Core depends on it; extensions depend on it; it depends on neither.

## What is in it

| Module | Purpose |
| --- | --- |
| `lotek_kit.attackpath` | The `attackpath/v1` document model — `normalize()`, `blank_model()`, `is_supported_schema_id()`. Ported from `vector/vector/schema.py`. |
| `lotek_kit.assets` | Stdlib access to shipped browser assets, for inlining into a self-contained deliverable. |
| `lotek_kit.static/` | The browser assets themselves. |

## The admission rule

Something belongs in the kit when **two consumers that may not import each other both need it**. That is
the whole test. A helper only scribble uses belongs in scribble; convenience is not a reason to widen a
package that every consumer is forced to install.

Three hard constraints, each pinned by a test in `tests/`:

1. **No runtime dependencies.** Core takes this as a *base* dependency, so anything added here lands in
   lotek itself and in every extension downstream. Flask is an optional extra, imported inside the
   function that needs it.
2. **It never imports lotek or an extension.** Not at module scope, not lazily.
3. **It cannot become an extension.** No `lotek-extension.toml`, no `lotek.extensions` entry point, no
   `register()`. lotek's discovery enumerates exactly that entry-point group, so their absence is what
   makes this package structurally unmountable rather than merely unmounted.

## Versioning

There is no `version` literal. Every other subproject in this monorepo is frozen at `0.1.0.dev0` with the
real version derived from the git build id, so a hand-bumped semver here would be the only one and would
drift the first time someone forgot it. `hatch_build.py` stamps the same dated
`YYYY.M.D.HHMMSS+g<shorthash>` string `scripts/build_id.py` produces, and a test asserts the two agree.

## How consumers pin it

```toml
# an extension, or anything else in this monorepo
dependencies = ["lotek-kit"]
[tool.uv.sources]
lotek-kit = { path = "../kit", editable = true }
```

`editable = true` is load-bearing, not cosmetic — a non-editable path install is cached by version, so
after editing kit source `uv sync` reports "Audited" and does not rebuild (the trap documented in the
repo's `CLAUDE.md`). Because the kit ships no force-included files, editable is safe here in a way it is
not for an extension.

lotek core pins it as a git dependency on a release tag, in its **base** dependencies rather than the
`extensions` extra — core's PR-gate CI lane runs `uv sync --extra dev` and installs no extensions, so a
kit in that extra would be absent on the lane that gates every PR.

**Core's tag and every kit-consuming extension's tag must match.** One process has exactly one
`lotek_kit` module, so two consumers on different kits is a bug rather than a configuration. If they
diverge, `uv lock` fails loudly with `Requirements contain conflicting URLs for package 'lotek-kit'`
naming both tags — the disagreement cannot reach runtime.

## What is deliberately absent

**A version-guard helper.** The obvious design — `require("x.y")` called from an extension's
`register()` — is a security hole, not a safety net. By the time `register()` can raise,
`app.register_blueprint` has already run and is irreversible; the host's `_inject_host` (the only writer
of the authorization extras) has *not* yet run; and `mount_extensions` catches the exception and carries
on. That leaves a mounted surface with no injected authorization. Skew is prevented by the pin recipe
above instead, where it fails at lock time rather than at request time.
