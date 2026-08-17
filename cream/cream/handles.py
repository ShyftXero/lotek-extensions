"""Human-readable handles for a document that has no number yet.

A document is numbered at issue time (``service.issue`` assigns ``INV-2026-0001``); before that
``Document.number`` is NULL. The list used to render the empty cell as a bare ``—``, and because the
``Number`` cell is also the row's link, a draft's whole click target was one em-dash: no identity to
read, quote in an email, or aim a mouse at. A client reported exactly that (ext#46).

So an unissued document gets a **stable handle derived from its id** instead — ``draft …b839c91e20``.

**The tail, never the prefix.** These ids are UUIDv7: the leading 48 bits are a millisecond timestamp, so
consecutively created ids share their front. lotek#336 measured five created in one loop sharing their
first *23* characters — 1 distinct first-8 out of 5, 5 distinct last-8 out of 5. A "short id" taken from
the head therefore shows different documents as the same string, and it fails **silently**: nothing
errors, the reader just quotes the wrong invoice. The discriminating bits are at the end. That is the same
property that makes ``ORDER BY id`` recover creation order, so it is not going to change.

The display convention (tail, leading ``…``, full id available on hover) is lotek#336's, deliberately
rather than a second one invented here; only the *length* differs, and for a stated reason — see
:data:`TAIL_LEN`.
"""

from __future__ import annotations

import uuid

#: Leading marker on a truncated id, so a handle can never be mistaken for a whole one.
ELLIPSIS = "…"

#: Hex characters of the id's tail to show.
#:
#: lotek#336's widget defaults to 6, where the short id is a *secondary* discriminator sitting beside a
#: friendly name (``Acme Corp · …841044``) and only has to separate look-alikes. Here the handle is the
#: entire contents of the identity column — there is no name beside it — and it is what a human quotes
#: back ("about draft …b839c91e20"), so it carries the whole burden. 10 is what the reporting client
#: asked for and what ext#46 specifies; #336 makes the length a per-call-site knob precisely so a call
#: site like this one can widen it.
TAIL_LEN = 10


def uuid_tail(value: uuid.UUID | str | None, length: int = TAIL_LEN) -> str:
    """``…b839c91e20`` — the tail of an id, marked as a fragment.

    Returns ``""`` for a missing id: a handle for nothing is worse than no handle, because it looks like
    an identifier and is not one. Dashes are kept as-is (a UUID's last group is 12 hex characters, so a
    tail of 12 or fewer never straddles one) and an id shorter than ``length`` is returned whole, with no
    ``…`` — nothing was elided.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) <= length:
        return text
    return f"{ELLIPSIS}{text[-length:]}"


def document_handle(number: str | None, status: str, doc_id: uuid.UUID | str | None) -> str:
    """What the list's identity cell says: the issued number, else ``<status> …<id tail>``.

    The status word — not the literal ``"draft"`` — carries the unissued/issued distinction the em-dash
    used to, because ``service.void`` accepts a *draft* as well as an issued document, so an unnumbered
    document is not necessarily a draft and labelling one ``draft …b839c91e20`` would misreport it.

    Falls back to the status alone when there is no id to truncate, and to :data:`ELLIPSIS` if there is
    neither — the cell stays non-empty so the row's link keeps a click target either way.
    """
    text = (number or "").strip()
    if text:
        return text
    tail = uuid_tail(doc_id)
    label = (status or "").strip()
    return " ".join(part for part in (label, tail) if part) or ELLIPSIS
