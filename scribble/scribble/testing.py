"""A mock lotek host — the shell that makes standalone Scribble a testbed rather than a second product.

Scribble has exactly one way to persist a file: the host's object store. That is the point — evidence
used to land on local disk or in the store depending on which route a human used, and the difference
was a source of bugs on its own (an empty evidence gallery that nothing went red for).

Keeping a disk fallback "for standalone" would put that difference straight back, so standalone is
not a supported deployment: it is a demo/testbed, and the thing that boots it supplies a MOCK host
that answers the same contract lotek does. One code path, exercised the same way everywhere.

What a host must provide for Scribble to accept a file:

* ``objects`` — the actor-gated object surface (``put``/``open``/``stat``/``delete``/``list``);
* ``create_engagement`` — the CORE engagement a blob is anchored to (``objects.engagement_id`` is NOT
  NULL for every blob; INV-OBJSTORE-01 makes that a database fact via composite FKs).

`InMemoryObjects` MIRRORS the real surface's REFUSALS, not just its happy path. A permissive fake is
worse than none here: an earlier version ignored its ``actor`` argument, and a change that would have
raised ``PermissionError`` on every browser upload in production passed its tests green. Anything the
real `HostObjects` refuses, this refuses.

NOT a substitute for the real thing. It holds blobs in memory, so a demo shell loses them on restart,
and it applies no tenancy seam of its own beyond "an actor is present" — a real host resolves live
memberships per call. It exists so Scribble's own code has one shape, not to prove lotek's.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

#: Core's ``ObjectKind`` members. Mirrored rather than imported — an extension must never import a
#: host module — and enforced below, because core validates the kind and a non-member is refused.
CORE_OBJECT_KINDS = frozenset({"artifact", "report", "screenshot", "evidence"})


@dataclass
class StoredBlob:
    kind: str
    data: bytes
    content_type: str
    filename: str
    engagement_id: Any
    created_by: Any
    deleted: bool = False


@dataclass
class InMemoryObjects:
    """The host object surface, in memory, refusing what the real one refuses."""

    blobs: dict[uuid.UUID, StoredBlob] = field(default_factory=dict)
    puts: list[StoredBlob] = field(default_factory=list)
    #: Flip to make every put raise ``PermissionError``, as the real surface does for an actor with no
    #: operator capability on the engagement.
    refuse_put: bool = False

    def put(self, actor, *, kind, stream, content_type, filename, job_id=None, engagement_id=None):
        if actor is None or self.refuse_put:
            # A None principal holds zero engagements in core, so it can neither read nor write.
            raise PermissionError("not an operator on the engagement")
        if (job_id is None) == (engagement_id is None):
            raise ValueError("exactly one of job_id / engagement_id is required")
        if kind not in CORE_OBJECT_KINDS:
            raise ValueError(f"invalid kind {kind!r}")
        data = stream.read()
        object_id = uuid.uuid7()
        blob = StoredBlob(kind, data, content_type, filename, engagement_id, actor)
        self.blobs[object_id] = blob
        self.puts.append(blob)
        # No ``s3_key`` on the ref: the store stays dashboard-only.
        return SimpleNamespace(id=object_id, kind=kind, byte_size=len(data),
                               sha256=hashlib.sha256(data).hexdigest())

    def open(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            # Absent and not-visible are ONE answer, as in core — so a caller cannot become an
            # existence oracle for another engagement's evidence.
            raise KeyError(object_id)
        return io.BytesIO(blob.data)

    def stat(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            return None
        return SimpleNamespace(id=object_id, byte_size=len(blob.data), kind=blob.kind)

    def delete(self, actor, object_id) -> bool:
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            return False
        blob.deleted = True  # a tombstone, as in core — a GC reclaims the bytes later
        return True

    def list(self, actor, **_kw):
        if actor is None:
            return []
        return [SimpleNamespace(id=k) for k, v in self.blobs.items() if not v.deleted]


@dataclass
class MockCoreEngagements:
    """``create_engagement`` — the anchor factory, with core's privilege rule.

    Core refuses this to anyone below manager/admin ("establishing engagement tenancy is deliberately
    privileged"). The mock refuses too, and `allow_create` is how a test drives that arm: a shell that
    always said yes would let a privilege regression through as a pass.
    """

    created: list[tuple[Any, str]] = field(default_factory=list)
    allow_create: bool = True
    #: Names already taken per client — core's uniqueness rule is per client, not global.
    _taken: set[tuple[Any, str]] = field(default_factory=set)

    def create_engagement(self, client_id, name):
        if not self.allow_create:
            raise PermissionError("not permitted to create an engagement for this client")
        # Uniqueness is PER CLIENT in core, so with no client there is no namespace to clash in.
        # Enforcing a global one would be the mock inventing a rule core does not have, and failing
        # tests for a reason production never produces.
        if client_id is not None and (client_id, name) in self._taken:
            raise ValueError("an engagement with this name already exists for this client")
        self._taken.add((client_id, name))
        engagement_id = uuid.uuid7()
        self.created.append((client_id, name))
        return engagement_id


def wire_mock_host(cfg, *, actor=None, objects=None, engagements=None):
    """Fill ``cfg.extras`` with the minimum a host must provide for Scribble to accept a file.

    Deliberately ADDITIVE — it never clears keys another harness already set, so a richer stub host can
    wire its own authorization hooks and still get an object surface from here.

    Returns ``(objects, engagements)`` so a caller can assert against them.
    """
    objects = objects if objects is not None else InMemoryObjects()
    engagements = engagements if engagements is not None else MockCoreEngagements()
    extras = cfg.extras
    extras["objects"] = objects
    extras["create_engagement"] = engagements.create_engagement
    # Deliberately NOT `extras["host"]`. That key is the truthy marker `authz.host_is_mounted()` reads
    # to mean "a lotek authorization model applies here", and setting it would make every standalone
    # test look mounted — then `can_view_client_id` finds no `can_view_client` hook, fails closed, and
    # the whole suite 404s. Storage and authorization are separate capabilities and this supplies only
    # the first.
    #
    # `pat_actor` IS defaulted, because the object surface authorizes against a principal and a None
    # actor is refused by design.
    extras.setdefault("pat_actor", lambda: actor if actor is not None else SimpleNamespace(
        id=uuid.uuid7(), username="mock-operator", role="operator", scopes=frozenset({"read", "write"})))
    return objects, engagements


def store_evidence(app, filename: str, data: bytes, *, core_engagement_id=None,
                   content_type: str | None = None) -> tuple[str, str, int]:
    """Put bytes where an upload would, and return ``(reference, sha256, byte_size)``.

    The testbed replacement for the old ``save_bytes``: a test that needs an artifact row wants the
    bytes reachable, not a file on disk. Going through the real ``persist_bytes`` is the point — a
    helper that wrote straight into the mock store would let the production write path rot untested.

    ``core_engagement_id`` defaults to a fresh id: most callers only need SOME anchor, and inventing
    one here keeps them from having to build a mapped engagement they do not otherwise care about.
    """
    from .artifacts_storage import persist_bytes

    with app.app_context():
        return persist_bytes(
            core_engagement_id=core_engagement_id or uuid.uuid7(),
            filename=filename, data=data, content_type=content_type,
        )


def read_evidence(app, reference: str) -> bytes | None:
    """The bytes behind a reference, or None — the testbed replacement for resolving a disk path."""
    from .artifacts_storage import artifact_bytes

    with app.app_context():
        return artifact_bytes(reference)
