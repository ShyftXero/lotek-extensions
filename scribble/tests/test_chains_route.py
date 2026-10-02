"""``POST /scribble/machine/engagements/<id>/chains`` - mint an attack-chain narrative from deterministic
exploiteer output (LOT-69), the machine caller the render side (``reporting/context.py::_chain_ctxs``) was
already waiting on.

Sibling of ``scribble_link_attack_path`` and modeled on it, so the properties proven here are the same
three that route carries, plus the two this one adds:

  1. TENANCY-BEFORE-BODY (INV-TENANCY-05): a caller who is not an operator on the engagement is refused
     403 BEFORE the body is parsed - a malformed body still answers 403, never 400, and writes nothing.
  2. IDEMPOTENCY: the same ``idempotency_key`` replays the original chain (200), mints no duplicate.
  3. PROVENANCE (evidence-first): ``source_finding_ids`` / ``rule_ids`` round-trip through the new columns.
  4. BYTE-FOR-BYTE step prose: a step's ``description`` is stored exactly as sent (no strip, no paraphrase)
     - this endpoint synthesizes no prose (LOT-44 no-LLM-in-render doctrine).

The engagement-wide tenancy sweeps (``test_scribble_machine_tenancy.py``) auto-discover this route off
``app.url_map`` and already prove the foreign-client 404 and the read-only-token 403; this file is the
readable per-route control for the chain-specific behaviour those sweeps cannot express.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

import scribble.models as fm
from tests.conftest import StubActor

M = "/scribble/machine"

ACME = uuid.uuid7()          # the client the token under test operates on
OTHER_CLIENT = uuid.uuid7()  # a client it may view but not operate


def _engagement(session_factory, *, client_id) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Chain target", client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _operator(stub_host) -> None:
    """A write-scoped token that IS an operator on this engagement - the granted caller."""
    stub_host.actor = StubActor(id=7, username="chain-tool", role="operator")
    stub_host.viewable_client_ids = {ACME}
    stub_host.can_operate_value = True


def _chains(session_factory, engagement_id):
    with session_factory() as db:
        return list(db.get(fm.ReportBoard, engagement_id).chains)


# ── 1. tenancy-before-body ───────────────────────────────────────────────────────────────────────────


def test_out_of_scope_caller_is_refused_403_before_the_body_is_read(app, stub_host, session_factory):
    """A caller who MAY view the engagement but is NOT an operator on it is refused 403 - and the refusal
    lands BEFORE body validation, so an otherwise-400 malformed body still answers 403 and writes nothing.

    Red → green: moving the ``_deny_write`` check to AFTER ``request.get_json`` turns this into a 400 (the
    body is bad), which is the id-space oracle INV-TENANCY-05 forbids."""
    eid = _engagement(session_factory, client_id=ACME)
    stub_host.actor = StubActor(id=9, username="viewer-tool", role="operator")
    stub_host.viewable_client_ids = {ACME}   # CAN view -> not a 404
    stub_host.can_operate_value = False       # but NOT an operator -> 403

    # A body that is ALSO malformed (no title): if the write axis ran after parsing, this would be a 400.
    resp = app.test_client().post(f"{M}/engagements/{eid}/chains", json={"steps": "not-a-list"})

    assert resp.status_code == 403, resp.get_json()
    assert _chains(session_factory, eid) == [], "a refused write must not mint a chain"


# ── 2. idempotency + provenance ──────────────────────────────────────────────────────────────────────


def test_same_idempotency_key_replays_the_original_and_persists_provenance(app, stub_host, session_factory):
    eid = _engagement(session_factory, client_id=ACME)
    _operator(stub_host)
    client = app.test_client()

    body = {
        "title": "Reflected XSS to domain admin",
        "summary": "Chained a reflected XSS into a session theft, then Kerberoasted to DA.",
        "steps": [
            {"title": "Initial access via reflected XSS", "description": "Payload in ?q=", "order_index": 0},
            {"title": "Kerberoast svc account", "description": "GetUserSPNs.py, cracked offline"},
        ],
        "source_finding_ids": [str(uuid.uuid7()), str(uuid.uuid7())],
        "rule_ids": ["CVE-2023-1234", "kerberoast", "nuclei:reflected-xss"],
        "idempotency_key": "chain-key-1",
    }

    first = client.post(f"{M}/engagements/{eid}/chains", json=body)
    assert first.status_code == 201, first.get_json()
    minted = first.get_json()
    assert minted["title"] == body["title"]
    assert [s["title"] for s in minted["steps"]] == [s["title"] for s in body["steps"]]

    # Same key, same request -> the ORIGINAL chain is replayed, not a duplicate minted. The mounted host
    # answers a replay 200; the test's in-memory idempotency stub faithfully replays the STORED response
    # (status included, so 201 here) - the invariant this proves is the reporter's own #114 acceptance for
    # the sibling attack-path route: id-equality + a single row, NOT the wire status the mount sets. That
    # "same key -> 200 original" is proven at the mount in core's suite.
    second = client.post(f"{M}/engagements/{eid}/chains", json=body)
    assert second.get_json()["id"] == minted["id"], "a retry must replay the ORIGINAL chain"

    rows = _chains(session_factory, eid)
    assert len(rows) == 1, "a retry with the same key must not mint a second chain"

    # Provenance columns persist (evidence-first: the chain can cite what it was minted from).
    with session_factory() as db:
        chain = db.get(fm.ReportBoard, eid).chains[0]
        assert chain.source_finding_ids == body["source_finding_ids"]
        assert chain.rule_ids == body["rule_ids"]
        assert [s.order_index for s in sorted(chain.steps, key=lambda s: s.order_index)] == [0, 1]


# ── 3. byte-for-byte step prose ──────────────────────────────────────────────────────────────────────


def test_step_description_is_stored_byte_for_byte(app, stub_host, session_factory):
    """A hop's ``description`` is the exploiteer's deterministic reproduction text: indentation, trailing
    newlines and tabs are evidence, not noise, so it is stored EXACTLY as sent (only NUL is escaped, which
    Postgres refuses raw). No ``.strip()``, no paraphrase."""
    eid = _engagement(session_factory, client_id=ACME)
    _operator(stub_host)

    exact = "  $ GetUserSPNs.py -request\n\t- svc_sql  (RC4)\n\nCracked: Summer2024!\n"
    resp = app.test_client().post(
        f"{M}/engagements/{eid}/chains",
        json={"title": "Repro", "steps": [{"title": "Kerberoast", "description": exact}]},
    )
    assert resp.status_code == 201, resp.get_json()

    with session_factory() as db:
        step = db.get(fm.ReportBoard, eid).chains[0].steps[0]
        assert step.description == exact, "step description must be byte-for-byte the request's"


# ── 4. the migration: additive, reversible, backward-safe ────────────────────────────────────────────


def _load_migration():
    path = (
        Path(fm.__file__).parent
        / "migrations" / "versions" / "b2c4e6f8a1d3_attack_chain_provenance.py"
    )
    spec = importlib.util.spec_from_file_location("_lot69_provenance_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_step(engine, fn):
    """Drive one migration function (``upgrade``/``downgrade``) against ``engine`` with the module-level
    ``alembic.op`` proxy bound to a real Operations context - the same proxy the migration imports."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def test_migration_is_additive_reversible_and_backward_safe(tmp_path):
    """A PRE-EXISTING deployment (attack-chain tables without the provenance columns, real rows in them) is
    upgraded ADDITIVELY - the four nullable columns appear, the existing rows survive and read NULL - and
    DOWNGRADE removes them cleanly, rows still intact. This is the one migration test that runs on the
    suite's own SQLite backend rather than being Postgres-gated: it drives the revision's own
    ``upgrade``/``downgrade`` directly, so a regression in either is caught here."""
    mig = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")

    # The pre-provenance shape of the two tables (migration d3f5a7c9b1e2), plus one real row in each.
    board_id = str(uuid.uuid7())
    chain_id = str(uuid.uuid7())
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE scribble_attack_chains ("
            " id CHAR(32) PRIMARY KEY, engagement_id CHAR(32), title VARCHAR(255), summary TEXT,"
            " diagram_ref VARCHAR(64), embed_html TEXT, order_index INTEGER, include_in_report BOOLEAN,"
            " created_at DATETIME, updated_at DATETIME)"
        ))
        c.execute(text(
            "CREATE TABLE scribble_attack_chain_steps ("
            " id CHAR(32) PRIMARY KEY, chain_id CHAR(32), order_index INTEGER, title VARCHAR(255),"
            " description TEXT, created_at DATETIME, updated_at DATETIME)"
        ))
        c.execute(
            text("INSERT INTO scribble_attack_chains "
                 "(id, engagement_id, title, order_index, include_in_report) "
                 "VALUES (:id, :eng, 'legacy chain', 0, 1)"),
            {"id": chain_id, "eng": board_id},
        )
        c.execute(
            text("INSERT INTO scribble_attack_chain_steps (id, chain_id, order_index, title) "
                 "VALUES (:id, :cid, 0, 'legacy step')"),
            {"id": str(uuid.uuid7()), "cid": chain_id},
        )

    def _cols(table):
        return {col["name"] for col in inspect(engine).get_columns(table)}

    # UPGRADE: additive - the four columns appear, nothing pre-existing is lost.
    _run_step(engine, mig.upgrade)
    assert {"source_finding_ids", "rule_ids"} <= _cols("scribble_attack_chains")
    assert {"finding_id", "rule_id"} <= _cols("scribble_attack_chain_steps")
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT title, source_finding_ids, rule_ids FROM scribble_attack_chains WHERE id = :id"
        ), {"id": chain_id}).first()
    assert row.title == "legacy chain", "the pre-existing row must survive the upgrade"
    assert row.source_finding_ids is None and row.rule_ids is None, "existing row reads NULL (backward-safe)"

    # Idempotent: a second upgrade (guarded on the reflected columns) must not raise.
    _run_step(engine, mig.upgrade)

    # DOWNGRADE: reversible - the columns are removed, the rows still there.
    _run_step(engine, mig.downgrade)
    assert not ({"source_finding_ids", "rule_ids"} & _cols("scribble_attack_chains"))
    assert not ({"finding_id", "rule_id"} & _cols("scribble_attack_chain_steps"))
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM scribble_attack_chains")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM scribble_attack_chain_steps")).scalar_one() == 1
