# pastel

Markdown pastes for lotek, behind unguessable GUID URLs — with optional per-paste **expiry**,
**read-count limit**, and **password** (HTTP Basic auth). A paste is created in the browser editor or
posted by a runner through the PAT machine API; readers open it by its GUID. The three access controls
compose, and every gate fails closed.

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `GET /pastel/` | session | Editor + your pastes |
| `POST /pastel/` | session | Create a paste |
| `GET /pastel/<id>` | session, owner | Owner preview (does **not** count as a read) |
| `POST /pastel/<id>/update` | session, owner | Edit title/body |
| `POST /pastel/<id>/delete` | session, owner | Delete |
| `GET /pastel/p/<guid>` | **none** (GUID is the credential) | Rendered public view — counts as a read |
| `GET /pastel/p/<guid>/raw` | **none** | Raw markdown — counts as a read |
| `… /pastel/machine/pastes` | PAT (Bearer) | Runner create / list / get / delete |

## Access controls (ShyftXero/lotek-extensions#369)

- **Expiry** — absolute or relative TTL (`1h`/`6h`/`1d`/`7d`/`30d` or seconds). Checked lazily on every
  read; a `sweep_expired()` helper exists for a periodic job but correctness does not depend on it. An
  expired paste is a flat 404 — indistinguishable from an unknown GUID (no existence oracle).
- **Read-count limit** — after N successful GUID fetches (rendered *or* `/raw`) the paste retires.
  Counting is a single atomic conditional `UPDATE`, so concurrent fetches cannot serve past the cap.
  The owner's own authed preview never counts.
- **Password** — stored as a werkzeug hash, constant-time verified; the GUID URL challenges with
  `WWW-Authenticate: Basic`. A wrong password never consumes a read. Applies to the rendered view and
  `/raw`. A runner can set a password at create time; a machine reader supplies it as HTTP Basic.

A retirement (expiry, read-limit, or an explicit delete) is audited with its cause and never its content.

## Develop

```sh
cd pastel
uv sync --extra dev
uv run --extra dev pytest -q
uvx ruff check .
```

Mounted into lotek via a pinned git dependency + the `lotek.extensions` entry point; the host reads
`lotek-extension.toml` from the built wheel. Use a **non-editable** path source for local dev (an
editable install skips the wheel build, so the manifest is absent and discovery silently skips it).
