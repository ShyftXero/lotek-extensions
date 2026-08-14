# lotek-extensions

The home for **lotek platform extensions** — mounted Flask sub-applications that add platform features on
top of the [lotek](https://github.com/ShyftXero/lotek) framework. One repo, one flow, all extensions.

| Extension | `/prefix` | What |
|---|---|---|
| **cream** | `/cream` | Client quoting + invoicing (SOW/quotes + invoices; no payment processing) |
| **registrar** | `/registrar` | Offensive-infra inventory + control (servers/domains/DNS, provider-agnostic) |
| **scribble** | `/scribble` | Pentest reporting + the vuln-enrichment proposal seam |
| **vector** | `/vector` | Attack-path (kill-chain) diagram editor + HTML deliverable |

Each is **bundled with lotek and enabled by default** — a fresh lotek install mounts all four out of the
box (opt out per extension with `[extensions] <name> = false` or the admin Settings → Extensions toggle).

## Layout

Each extension is a **self-contained subdirectory** — its own package, `pyproject.toml`,
`lotek-extension.toml` (the manifest lotek reads), and `docs/<NAME>.md` (its operator doc, shipped inside
the wheel and rendered on lotek's in-app Docs page):

```
lotek-extensions/
  cream/       pyproject.toml · lotek-extension.toml · README.md · docs/CREAM.md · cream/       (the package)
  registrar/   …               · …                    · …         · docs/…        · registrar/
  scribble/    …
  vector/      …
```

## The mount contract (v2-native)

An extension exposes `register(app, engine, *, session_factory, base_template, url_prefix,
create_tables, **host_models) -> Config` and reaches lotek **only** through the injected host contract
(`extras`), never by importing lotek internals. It must be v2-native:

- **UUIDv7 surrogate PKs**; cross-core references (`engagement_id`, `client_id`, `owner_id`, …) are
  **`sqlalchemy.Uuid`**, never Integer/String.
- **No authorization data in the extension** — core owns identity/authz/engagements; an extension
  *references* core and resolves engagement rights through the host seam
  (`can_operate_on` / `visible_engagement_ids`), never a request body.
- Own tables, **prefixed** (`cream_*`, `registrar_*`, …).

## How lotek consumes these (pinned git deps — vendoring is retired)

lotek installs each extension as a **pinned git dependency** from this public monorepo (one subdirectory
per extension) — there is **no vendored copy** in the lotek tree and **no `stage-extension.sh`** anymore.
lotek's `pyproject.toml` points `[tool.uv.sources]` at this repo per subdirectory, and discovery is by the
`lotek.extensions` entry point each package declares. From a lotek checkout:

```sh
uv sync --extra extensions                 # install/refresh all four (installs anonymously — repo is public)
uv lock --upgrade-package scribble         # bump the pin to this repo's latest for one extension
```

Each package self-describes to the host through its wheel-shipped `lotek-extension.toml` (`[mount]` /
`[[nav]]` / `[capabilities]` / `[host]` / `[db]`) and, via `[host] docs`, ships its operator doc inside the
wheel for lotek's Docs page to render. The provenance shown on lotek's admin Extensions page is the pinned
git commit (from PEP 610 `direct_url.json`).

## Working here

See [CLAUDE.md](CLAUDE.md) — this repo adopts lotek's flow (branch-per-change off `main` → PR →
squash-merge → delete; `plans/<branch>.md` first; ruff + the extension's tests green before the PR).
