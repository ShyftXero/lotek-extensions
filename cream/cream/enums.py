"""CREAM enumerations."""

from __future__ import annotations

import enum


class DocKind(enum.Enum):
    quote = "quote"      # a SOW / estimate, pre-engagement
    invoice = "invoice"  # bills actual work


class DocStatus(enum.Enum):
    draft = "draft"      # tracks its engagement live; fully editable
    issued = "issued"    # frozen, numbered, immutable snapshot
    sent = "sent"        # issued + delivered to the client
    void = "void"        # cancelled (issued docs are never deleted, only voided)
