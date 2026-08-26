# bugreport — bug report capture

A text-only place for **users and agents** to file "here is an aspect of a bug". It captures reports; it
does not forward them to GitHub, Jira, email or anywhere else. Tracks
[ShyftXero/lotek-extensions#112](https://github.com/ShyftXero/lotek-extensions/issues/112).

Mounts into lotek at `/bugreport` (or into any Flask app via `bugreport.register`).

## What each role can do

| | user (non-admin) | admin |
|---|---|---|
| file a report | ✅ | ✅ |
| read | own only | all |
| edit the text | own only, unless it was deleted | ❌ *(rewriting somebody else's words is not "admin CRUD")* |
| delete | own only — a real row delete | tombstone only: `status=deleted` + a note |
| set status / leave a note | ❌ | ✅ — this is the feedback the reporter reads |

**A report another user owns is `404`, never `403`** — a 403 confirms the id exists
(lotek `INVARIANTS.md` INV-TENANCY-01).

**An admin "delete" is a tombstone on purpose.** A removed row cannot tell its reporter it was removed,
and telling them is the whole point of the issue. The reporter sees *"An admin marked this deleted —
'<note>'"* and can then delete the tombstone themselves.

## Surfaces

* **`GET /bugreport/`** — the page. Plain HTML forms, no JavaScript: your reports (with any admin
  response) and, for an admin, everyone's with a status/note form per row.
* **`/bugreport/machine/*`** — the PAT/Bearer machine API, which is how an **agent** files a report:

  | route | scope | who |
  |---|---|---|
  | `GET /reports` | `read` | own, or all for an admin |
  | `POST /reports` | `write` | attributed to the token's user (there is no reporter field) |
  | `GET /reports/<id>` | `read` | own / admin, else 404 |
  | `PATCH /reports/<id>` | `write` | `title`/`body` = reporter edit · `status`/`note` = admin response. Not both in one call |
  | `DELETE /reports/<id>` | `write` | owner only, even for an admin (admins tombstone) |

Every list surface returns the **newest 500** (`service.LIST_LIMIT`). Filing is not rate-limited and a
body runs to 20 KB, so an uncapped admin list is a lever any authenticated user can pull.

## Data

One table, `bugreport_reports`. UUIDv7 PK; `reporter_id` is a **`sqlalchemy.Uuid` soft reference** to a
core `User` (never `Integer`/`String` — INV-INTEGRITY-03), with no FK, because the core table is unknown
until mount time. `reporter_name` is denormalised so a report stays attributable after the account is
gone. Schema is `create_all` + an additive ADD-COLUMN pass, like every sibling extension here.

## Deliberately not built

No outward filing, attachments, comment threads, severity/priority/labels, assignment, search,
pagination, notifications or unread badges, and no admin hard-purge. None is named in #112; see the PR
for when each would be worth adding.

## Develop

```sh
uv run --extra dev pytest -q       # from this directory
uvx ruff check bugreport
```

Mounting a local checkout into lotek: use a **non-editable** path source
(`bugreport = { path = "../lotek-extensions/bugreport" }`) and `uv sync --reinstall-package bugreport`.
`lotek-extension.toml` is force-included at *wheel-build* time, so `editable = true` silently skips it
and the extension never mounts.
