# pastel

A paste store for lotek: share text/markdown via an unguessable URL, with per-paste expiry,
read-count limits, and HTTP Basic password protection.

| Surface | Path |
|---|---|
| Paste list + create | `GET/POST /pastel/` |
| View paste | `GET /pastel/p/<slug>` |
| Raw text | `GET /pastel/p/<slug>/raw` |
| Delete paste | `POST /pastel/delete/<slug>` |
| Machine API | `POST/GET/DELETE /pastel/machine/pastes/…` |

## Access controls (lotek-extensions#369)

- **Expiry** — absolute deadline; expired pastes return 404 on first read (lazy retirement).
- **Read-count limit** — after N reads the paste is retired; the Nth read still serves content.
- **Password** — HTTP Basic auth challenge (`WWW-Authenticate: Basic`). Machine API uses the
  `X-Paste-Password` request header instead (Bearer and paste password do not share the
  `Authorization` field).

All three compose (a paste can be password-protected AND expiring AND read-limited); failure
on any one fails closed.

## Machine API (runner→pastel)

```bash
# Create
curl -X POST /pastel/machine/pastes \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{"body": "hello", "ttl_seconds": 3600, "read_limit": 5, "password": "s3cr3t"}'

# Read (password-protected)
curl /pastel/machine/pastes/<slug> \
  -H "Authorization: Bearer $PAT" \
  -H "X-Paste-Password: s3cr3t"

# Delete
curl -X DELETE /pastel/machine/pastes/<slug> \
  -H "Authorization: Bearer $PAT"
```

## Security notes

- Slug is `secrets.token_urlsafe(32)` (256-bit CSPRNG) — no time-ordered structure to guess.
- Password is Werkzeug `pbkdf2:sha256` + constant-time compare (via `werkzeug.security`).
- Read count is atomically incremented (single `UPDATE … RETURNING`) — no TOCTOU double-serve.
- Retired pastes (expired/read-limited/deleted) are indistinguishable from unknown slugs (404).
