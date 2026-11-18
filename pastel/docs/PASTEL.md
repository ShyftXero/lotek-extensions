# Pastel

Markdown pastes behind unguessable GUID URLs, with optional access controls.

## What it is

A paste is markdown stored against a 128-bit unguessable `guid`. The GUID — not the internal UUIDv7
primary key — is what a reader's URL carries, so the URL reveals no ordering and no count. Anyone with
the link can read the paste; there is no anonymous list, search, or metadata surface.

## Creating a paste

- **Browser:** the editor at `/pastel/` (title, markdown body, and the three optional access controls).
  The draft body autosaves to `localStorage` until you submit.
- **Runner (PAT):** `POST /pastel/machine/pastes` with `write` scope:

  ```json
  { "body": "# notes\n...", "title": "scan output",
    "expires_in": "1d", "max_reads": 5, "password": "s3cret" }
  ```

  The response carries the `guid`, the public `url`, and the `raw_url`.

## Reading a paste

- `GET /pastel/p/<guid>` — rendered (markdown → sanitized HTML).
- `GET /pastel/p/<guid>/raw` — the raw markdown source as `text/plain`.

Both are unauthenticated and both **count as a read**.

## Access controls (#369)

All optional, all compose, all fail closed. An expired / over-limit / unknown / retired GUID is an
identical flat **404** — no existence oracle. The one exception is a password-protected paste, which
answers **401** with `WWW-Authenticate: Basic` so a legitimate reader can authenticate.

### Expiry
Absolute (`expires_at`, ISO-8601) or relative (`expires_in`: `1h`/`6h`/`1d`/`7d`/`30d`, or integer
seconds). Checked **lazily on every read**; `service.sweep_expired()` exists for a periodic job but
correctness does not depend on it.

### Read-count limit
`max_reads = N` retires the paste after N successful GUID fetches (rendered *or* `/raw`). Counting is one
atomic conditional `UPDATE ... WHERE read_count < max_reads`, so under concurrent fetches the paste is
never served past the cap. The owner's authed preview (`/pastel/<id>`) never counts.

### Password
Stored as a werkzeug hash (salted, constant-time verified). Supplied as HTTP Basic — any username, the
password in the password field. A wrong or missing password returns 401 and **never consumes a read**
(the password gate runs before the counting update). Applies to both the rendered view and `/raw`. A
runner sets it at create time; a machine reader supplies it as Basic auth on the GUID URL.

## Audit

Retirement is audited with its cause only, never content: `retire` (`expired` | `read_limit`) for an
automatic retirement, `delete` for an explicit owner/admin delete.
