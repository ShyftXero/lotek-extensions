# Pastel — paste store

Pastel lets users and runners share text or markdown snippets via an unguessable URL, with
optional per-paste access controls: expiry, read-count limits, and password protection.

## Usage

Navigate to **Pastel** in the sidebar to list and create pastes. Each paste gets a
256-bit random slug URL (`/pastel/p/<slug>`) — there is no directory listing.

### Creating a paste

Fill in the body, then optionally set:

| Field | Effect |
|---|---|
| **Title** | Display name (not shown in the URL) |
| **Expires** | Paste disappears after this duration |
| **Max reads** | Paste disappears after N reads |
| **Password** | Browser prompts for a password via HTTP Basic auth |

### Access controls

All three controls compose: a paste can have an expiry AND a read limit AND a password.
Any failed gate returns 404 — there is no way to tell whether a slug ever existed.

**Expiry** — the paste is checked on every read (lazy). An expired paste is retired on
the first read-attempt after the deadline; no background sweep is required.

**Read-count limit** — the counter is incremented atomically, one per read. The Nth read
(the last one) succeeds and returns content; the (N+1)th gets 404.
Owner previews do not count toward the limit.

**Password** — the browser is challenged with `WWW-Authenticate: Basic`. Enter any
username and the paste's password. For the machine API, supply the password in the
`X-Paste-Password` request header instead.

### `/raw` view

`GET /pastel/p/<slug>/raw` returns the body as `text/plain; charset=utf-8`. Useful for
piping into shell commands or curling from an agent.

## Machine API (runner → pastel)

Requires a lotek PAT with `write` scope (for create/delete) or `read` scope (for GET).

```
POST   /pastel/machine/pastes          create a paste
GET    /pastel/machine/pastes/<slug>   read a paste
DELETE /pastel/machine/pastes/<slug>   delete a paste
```

### Create

```json
POST /pastel/machine/pastes
Authorization: Bearer <pat>
Content-Type: application/json

{
  "body": "hello from an agent",
  "title": "optional title",
  "ttl_seconds": 3600,
  "read_limit": 1,
  "password": "optional-password"
}
```

Alternatively, pass `"expires_at": "<ISO 8601 timestamp>"` instead of `"ttl_seconds"`.

Response: `201 {"paste": {...}}` including the `slug` field.

### Read

```
GET /pastel/machine/pastes/<slug>
Authorization: Bearer <pat>
X-Paste-Password: optional-password   # only needed if the paste has a password
```

Response: `200 {"paste": {...}}` or `401 {"error":"password_required"}`.

### Delete

```
DELETE /pastel/machine/pastes/<slug>
Authorization: Bearer <pat>
```

## Security

- **Slug** is `secrets.token_urlsafe(32)` (256-bit CSPRNG). Never a UUID — UUIDv7 embeds
  a millisecond timestamp that leaks when the paste was created and shrinks the search space.
- **Password** is stored as a Werkzeug `pbkdf2:sha256` hash. Comparison is constant-time.
- **Retired pastes** (expired, read-limit hit, deleted) return 404 — identical to unknown
  slugs. No existence oracle.
- **Audit trail** — `ext:pastel:create`, `ext:pastel:delete`, `ext:pastel:retire` are
  written to lotek's audit log.
