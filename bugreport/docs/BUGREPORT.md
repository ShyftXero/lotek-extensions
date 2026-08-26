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

## Read-only accounts

A viewer-role account can read its own reports but cannot file or change one — the same rule the rest of
the dashboard applies to writes.
