# lotek-extensions

The home for **lotek platform extensions** — mounted Flask sub-applications that add platform features on
top of the [lotek](https://github.com/ShyftXero/lotek) framework. One repo, one flow, all extensions.

| Extension | `/prefix` | What |
|---|---|---|
| **cream** | `/cream` | Client quoting + invoicing (SOW/quotes + invoices; no payment processing) |
| **registrar** | `/registrar` | Offensive-infra inventory + control (servers/domains/DNS, provider-agnostic) |
| **scribble** | `/scribble` | Pentest reporting + the vuln-enrichment proposal seam |
| **vector** | `/vector` | Attack-path (kill-chain) diagram editor + HTML deliverable |

## Layout

Each extension is a **self-contained subdirectory** — its own package, `pyproject.toml`, and
`lotek-extension.toml` (the manifest lotek reads) — so it vendors into lotek unchanged:

```
lotek-extensions/
  cream/       pyproject.toml · lotek-extension.toml · README.md · cream/        (the package)
  registrar/   …               · …                    · …         · registrar/
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

## Vendoring into lotek

lotek bundles a snapshot of each extension in-tree. From the lotek checkout:

```sh
scripts/stage-extension.sh ~/Dropbox/code/lotek-extensions/cream cream cream /cream cream.seed:seed_defaults
scripts/stage-extension.sh ~/Dropbox/code/lotek-extensions/registrar registrar registrar /registrar
# …then: uv lock, enable in config.toml [extensions], uv sync --extra dev --extra extensions
```

The subdir (not the repo root) is the source: it carries the package + `pyproject.toml` the script needs;
the provenance SHA is this repo's `HEAD`.

## Working here

See [CLAUDE.md](CLAUDE.md) — this repo adopts lotek's flow (branch-per-change off `main` → PR →
squash-merge → delete; `plans/<branch>.md` first; ruff + the extension's tests green before the PR).
