"""CREAM enumerations."""

from __future__ import annotations

import enum


class DocKind(enum.Enum):
    quote = "quote"      # a SOW / estimate, pre-engagement
    invoice = "invoice"  # bills actual work


class DocStatus(enum.Enum):
    draft = "draft"        # tracks its engagement live; fully editable
    issued = "issued"      # frozen, numbered, immutable snapshot
    sent = "sent"          # issued + delivered to the client
    accepted = "accepted"  # a QUOTE the client approved — the state that unlocks conversion to an invoice
    void = "void"          # cancelled (issued docs are never deleted, only voided)


#: Units of measure a line item can be billed in. Deliberately a suggestion list backing a free-text
#: column, not a DB enum: flat-rate vs hourly is ``qty`` + ``unit``, not two code paths, and a firm that
#: bills per-endpoint or per-repo should not need a schema migration to say so.
COMMON_UNITS = ("project", "hr", "day", "week", "host", "app", "endpoint", "user", "repo", "each")
DEFAULT_UNIT = "project"
