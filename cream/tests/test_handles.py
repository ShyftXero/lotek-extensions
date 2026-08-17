"""The unissued-document handle — ``draft …b839c91e20`` (ext#46).

The property under test is not "it truncates". It is that the truncation lands on the part of a UUIDv7
that actually **discriminates**, and that an issued number is never overwritten by a handle.
"""

from __future__ import annotations

import uuid

from cream.handles import ELLIPSIS, TAIL_LEN, document_handle, uuid_tail

#: Two ids from lotek#336's measurement of five consecutively created UUIDv7s. They share their first
#: **23** characters — the leading 48 bits are a millisecond timestamp — and differ only at the end.
#: Hard-coded rather than generated so this stays a deterministic statement about the format.
SIBLING_A = "01a00c03-85aa-777f-a54a-c18514841044"
SIBLING_B = "01a00c03-85aa-777f-a54a-c18688046a74"

#: The draft from the client session that reported the em-dash cell (ext#46).
REPORTED = "01a00ff7-8e63-70a9-9e7c-ddb839c91e20"


def test_the_tail_is_marked_as_a_fragment():
    assert uuid_tail(REPORTED) == "…b839c91e20"
    assert uuid_tail(REPORTED).startswith(ELLIPSIS)
    assert len(uuid_tail(REPORTED)) == TAIL_LEN + len(ELLIPSIS)


def test_it_accepts_a_uuid_object_not_just_a_string():
    value = uuid.UUID(REPORTED)
    assert uuid_tail(value) == uuid_tail(REPORTED)


def test_the_handle_discriminates_ids_that_share_23_leading_characters():
    """The whole reason for taking the tail. A head-based short id makes these two the SAME string, and
    it fails silently — nobody sees an error, they just quote the wrong invoice."""
    assert SIBLING_A[:23] == SIBLING_B[:23]
    a = document_handle(None, "draft", SIBLING_A)
    b = document_handle(None, "draft", SIBLING_B)
    assert a != b, "two documents created in the same millisecond got the same handle"


def test_freshly_minted_uuid7s_all_get_distinct_handles():
    ids = [uuid.uuid7() for _ in range(5)]
    handles = {document_handle(None, "draft", i) for i in ids}
    assert len(handles) == len(ids)


def test_an_unissued_document_gets_its_status_and_its_id_tail():
    assert document_handle(None, "draft", REPORTED) == "draft …b839c91e20"


def test_an_issued_number_is_never_replaced_by_a_handle():
    assert document_handle("INV-2026-0007", "issued", REPORTED) == "INV-2026-0007"


def test_a_blank_number_counts_as_unissued():
    """``number`` is NULL before issue, but a whitespace string must not slip through as an identifier."""
    assert document_handle("", "draft", REPORTED) == "draft …b839c91e20"
    assert document_handle("   ", "draft", REPORTED) == "draft …b839c91e20"


def test_the_label_is_the_status_not_the_literal_word_draft():
    """``service.void`` accepts a *draft*, so an unnumbered document is not necessarily a draft. Calling
    a voided one ``draft …`` would state something false about it."""
    assert document_handle(None, "void", REPORTED) == "void …b839c91e20"


def test_a_missing_id_yields_no_fake_identifier():
    """Better a bare status than a handle for nothing: a plausible-looking id that identifies nothing is
    worse than an obviously empty cell."""
    assert uuid_tail(None) == ""
    assert uuid_tail("") == ""
    assert document_handle(None, "draft", None) == "draft"
    assert document_handle(None, "", None) == ELLIPSIS  # never empty — the cell is a link


def test_an_id_shorter_than_the_tail_is_not_dressed_up_as_a_fragment():
    assert uuid_tail("abc") == "abc"
    assert ELLIPSIS not in uuid_tail("abc")
