# Bug reports

A text box for anything that looks wrong. Reports are **captured here** — they are not filed into
GitHub, Jira or a ticket queue, and nothing leaves this instance.

## Filing one

Open **Bug reports** in the sidebar, write a one-line title and as much detail as you like, and submit.
That is the whole flow — there are no attachments, severities, labels or assignees to fill in.

Agents file the same way over the API:

```sh
curl -sX POST https://<host>/bugreport/machine/reports \
     -H "Authorization: Bearer $LOTEK_PAT" -H 'Content-Type: application/json' \
     -d '{"title":"scan wedges on empty pipeline","body":"steps: ..."}'
```

## What happens next

An admin sets a status and leaves a note. **You see both on your own report**, highlighted:

> An admin marked this **acknowledged**. “Reproduced — tracking it as #481.”

The statuses are `open` → `acknowledged` / `resolved` / `deleted`.

**If an admin deletes your report it stays in your list, marked `deleted`, with their reason.** That is
deliberate: a row that vanished could not tell you it had been removed. Once you have read it, you can
delete the tombstone yourself.

## What you can and cannot do

You can edit or delete **your own** reports at any time (an admin-deleted one can no longer be edited,
only removed). You cannot see, edit or delete anyone else's — someone else's report id looks exactly
like an id that does not exist, on purpose.

Admins see every report and respond to it. An admin cannot rewrite the text of your report; they can
only respond to it.

## Limits

Text only: a 200-character title and a 20,000-character body, and nothing else — no files, no images,
no links that get fetched. Lists show the newest 500 reports.

## Read-only accounts

A viewer-role account can read its own reports but cannot file or change one — the same rule the rest of
the dashboard applies to writes.

## Attaching files

A report can carry files — screenshots, logs, captures, anything. The bytes live in lotek's object
store (SeaweedFS) through the host seam `extras["blobs"]`, under a key namespace reserved for this
extension; the row here is just metadata. Bounds: **25 MiB per file**, **20 files per report**, both
enforced server-side while the upload streams (never from `Content-Length`, which the uploader also
controls).

**Browser:** the *Files* block on each of your reports — attach, download, share, delete.
**Machine (PAT):**

```
GET    /bugreport/machine/reports/<report_id>/attachments
POST   /bugreport/machine/reports/<report_id>/attachments   (multipart, field name: file)
GET    /bugreport/machine/attachments/<id>
DELETE /bugreport/machine/attachments/<id>
POST   /bugreport/machine/attachments/<id>/share            -> {"share_token": ..., "share_path": "/s/..."}
DELETE /bugreport/machine/attachments/<id>/share
```

Who can see a file is decided exactly where the report's own visibility is decided — **its reporter and
an admin, nobody else** — and a file you may not see is a `404`, never a `403`, so the response never
confirms it exists.

### Public share links

A file is **private when uploaded**. The owner (or an admin) can mint a link that works for anyone who
has it, with no login:

```
https://<lotek>/bugreport/s/<token>
```

Three things worth understanding about that link:

- **The URL is the credential.** Anyone holding it can fetch that one file. Treat it like a password.
- **The token is not a UUID, deliberately.** lotek keys database rows on UUIDv7, which is a millisecond
  timestamp plus a monotonic counter — ordered and time-correlated. That is fine for a primary key and
  a poor secret, because knowing roughly when a file was uploaded shrinks the guessing space. The share
  token is `secrets.token_urlsafe(32)`: 256 bits from the system CSPRNG, with no structure to exploit.
- **Revoke by rotating.** *Stop sharing* removes the link entirely; *New link* mints a fresh token, and
  every previously-issued URL stops working immediately. If an admin tombstones the report, its shared
  links stop resolving too.

The share route is the only unauthenticated surface this extension has, it is declared in the manifest
(`[host] public_prefix = "/s"`) and validated by the host to be a strict sub-path of `/bugreport`, and it
is strictly single-file: a full token returns one file, and there is no anonymous listing, counting,
searching or enumeration anywhere.

### What a browser is allowed to do with an uploaded file

The extension accepts arbitrary bytes and serves them from lotek's own origin, so nothing user-supplied
is ever allowed to execute there:

- The served `Content-Type` is chosen **server-side from the file's magic bytes**, never from the
  uploader's claim. A `.html` page labelled `image/png` is stored and served as
  `application/octet-stream` and downloads.
- Only four formats are ever rendered inline — PNG, JPEG, GIF, WebP — and only when the bytes agree
  with the label. **SVG is never inline**: it is a script-capable document.
- Every download carries `Content-Disposition` (attachment unless it is a verified image),
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`,
  `Referrer-Policy: no-referrer` (so a share token never leaks in a `Referer`) and `Cache-Control:
  no-store`.
- The uploaded filename is used only for the download name. The stored object key comes from the row's
  own UUID, so a filename containing path separators has nowhere to go.

### What happens to the bytes if something goes wrong

Attachments live in lotek's object store under a namespace reserved for this extension, and carry no
core `objects` row — which means core's own garbage collector structurally cannot see them. Two things
close that gap:

- If the database commit fails after the bytes are stored, this extension deletes the blob itself before
  re-raising. It is the only party that knows the key at that moment.
- For everything else (a killed process, a host that died mid-request) the manifest declares
  `[host] blob_claims`, so lotek periodically asks which blob ids still have rows here and reclaims the
  rest. It only ever asks about blobs older than a day, so a live upload is never swept, and it only asks
  extensions that are currently mounted — a disabled extension is skipped rather than purged.
