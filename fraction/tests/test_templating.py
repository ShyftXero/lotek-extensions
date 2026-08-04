"""WS6 tests: built-in resolution, custom-variable overlay + precedence, preview/lint, and the
`POST /preview` endpoint.

Fixtures (`app`, `client`, `session_factory`) come from `tests/conftest.py` (Sprint 0 scaffold): each
gets a fresh Flask app + SQLite engine per test, seeded with `seed_defaults` (which seeds the builtin
`TemplateVariable` rows WS6's precedence tests rely on).

The `/preview` endpoint tests use their own isolated Flask app + fresh `Blueprint` instances (the
`preview_client` fixture below) rather than `fraction.api.api_bp`/`fraction.blueprint.bp`: those module-
level singletons get registered onto an app by other tests' `app` fixture in the same process, and Flask
permanently forbids adding routes to a `Blueprint` once it has been registered once
(`_got_registered_once`). Building a throwaway pair of blueprints exercises `templating_api.register`
identically (it only touches the objects it's handed) without depending on test ordering or mutating
shared state other test modules rely on.
"""

from __future__ import annotations

import pytest
from flask import Blueprint, Flask
from sqlalchemy import create_engine

from fraction.config import FractionConfig
from fraction.content import schema
from fraction.db import create_all, make_session_factory
from fraction.enums import Severity, VariableScope, VariableType
from fraction.models import Engagement, EngagementFinding, TemplateVariable, VariableValue
from fraction.seed import seed_defaults
from fraction.templating import (
    BUILTIN_KEYS,
    build_context,
    build_full_context,
    lint_doc,
    lint_text,
    resolve_finding,
    resolve_text,
)
from fraction.templating_api import register as register_templating_api


@pytest.fixture
def preview_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'preview.db'}", future=True)
    create_all(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        seed_defaults(session, import_library=False)
        session.commit()
    return sf


@pytest.fixture
def preview_client(tmp_path, preview_session_factory):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    app.extensions["fraction"] = FractionConfig(
        session_factory=preview_session_factory, engine=None, instance_path=tmp_path
    )

    api_bp = Blueprint("fraction_api", __name__)
    ui_bp = Blueprint("fraction", __name__)
    register_templating_api(api_bp, ui_bp)  # the function under test
    app.register_blueprint(api_bp, url_prefix="/fraction/api")

    return app.test_client()


# --------------------------------------------------------------------------- helpers


def _engagement(**overrides) -> Engagement:
    data = dict(name="Q1 Pentest", company_name="Acme Corp")
    data.update(overrides)
    return Engagement(**data)


def _finding(engagement, **overrides) -> EngagementFinding:
    data = dict(engagement=engagement, title="SQLi", severity=Severity.high)
    data.update(overrides)
    return EngagementFinding(**data)


def _custom_variable(session, key, *, scope=VariableScope.engagement) -> TemplateVariable:
    var = TemplateVariable(key=key, label=key.title(), scope=scope, value_type=VariableType.str_)
    session.add(var)
    session.flush()
    return var


# --------------------------------------------------------------------------- built-in resolution


def test_builtin_resolution(session_factory):
    with session_factory() as db:
        eng = _engagement(name="Acme Q1")
        finding = _finding(eng, target_host="10.0.0.5", target_port="443")
        db.add(eng)
        db.commit()

        ctx = build_context(eng, finding)
        assert ctx["COMPANY_NAME"] == "Acme Corp"
        assert ctx["ENGAGEMENT_NAME"] == "Acme Q1"
        assert ctx["TARGET_HOST"] == "10.0.0.5"
        assert ctx["TARGET_PORT"] == "443"
        assert ctx["SEVERITY"] == "high"

        text = "{{COMPANY_NAME}} target: {{TARGET_HOST}}:{{TARGET_PORT}} ({{SEVERITY}})"
        assert resolve_text(text, ctx) == "Acme Corp target: 10.0.0.5:443 (high)"


def test_build_full_context_matches_builtins_when_no_custom_vars(session_factory):
    with session_factory() as db:
        eng = _engagement()
        db.add(eng)
        db.commit()

        assert build_full_context(db, eng) == build_context(eng)


# --------------------------------------------------------------------------- custom variable overlay


def test_custom_variable_overlay_engagement_scope(session_factory):
    with session_factory() as db:
        eng = _engagement()
        db.add(eng)
        db.flush()

        var = _custom_variable(db, "APP_NAME")
        db.add(VariableValue(variable=var, engagement_id=eng.id, value="Acme Portal"))
        db.commit()

        ctx = build_full_context(db, eng)
        assert ctx["APP_NAME"] == "Acme Portal"
        # built-ins remain intact alongside the overlay
        assert ctx["COMPANY_NAME"] == "Acme Corp"


def test_finding_scope_overrides_engagement_scope(session_factory):
    with session_factory() as db:
        eng = _engagement()
        finding = _finding(eng)
        db.add(eng)
        db.add(finding)
        db.flush()

        var = _custom_variable(db, "ENV_LABEL", scope=VariableScope.finding)
        db.add(VariableValue(variable=var, engagement_id=eng.id, value="staging"))
        db.add(VariableValue(variable=var, finding_id=finding.id, value="production"))
        db.commit()

        # Engagement-level alone (no finding) sees the engagement-scope value.
        eng_only_ctx = build_full_context(db, eng)
        assert eng_only_ctx["ENV_LABEL"] == "staging"

        # With the finding, the finding-scope value wins.
        finding_ctx = build_full_context(db, eng, finding)
        assert finding_ctx["ENV_LABEL"] == "production"


def test_builtin_key_not_silently_overwritten_by_custom_value(session_factory):
    """A VariableValue bound to a *builtin* TemplateVariable (e.g. the seeded COMPANY_NAME row) must not
    silently override the structurally-computed built-in value -- only load_variable_values-excluded
    (builtin=False) rows are overlaid automatically. See fraction/templating/context.py precedence."""
    with session_factory() as db:
        eng = _engagement(company_name="Acme Corp")
        db.add(eng)
        db.flush()

        builtin_company_var = db.query(TemplateVariable).filter_by(key="COMPANY_NAME").one()
        assert builtin_company_var.builtin is True
        db.add(VariableValue(variable=builtin_company_var, engagement_id=eng.id, value="Sneaky Override"))
        db.commit()

        ctx = build_full_context(db, eng)
        assert ctx["COMPANY_NAME"] == "Acme Corp"  # structural value wins, not the VariableValue row

        # An explicit `extra` override is the sanctioned way to override a builtin for one render.
        overridden = build_full_context(db, eng, extra={"COMPANY_NAME": "Explicit Override"})
        assert overridden["COMPANY_NAME"] == "Explicit Override"


# --------------------------------------------------------------------------- foreign/invalid tokens


def test_foreign_token_alone_survives_verbatim(session_factory):
    with session_factory() as db:
        eng = _engagement()
        db.add(eng)
        db.commit()
        ctx = build_context(eng)
        assert resolve_text("Password policy: {{.pass_pol}}", ctx) == "Password policy: {{.pass_pol}}"


def test_foreign_token_blocks_resolution_of_whole_string(session_factory):
    """Documented, frozen resolver.resolve_text behavior: if a string fails to compile as Jinja (a
    foreign/invalid token like `{{.pass_pol}}` makes the whole thing invalid), the ENTIRE string is
    returned unresolved -- not just the invalid token -- even if a valid token like {{COMPANY_NAME}} is
    also present."""
    with session_factory() as db:
        eng = _engagement()
        db.add(eng)
        db.commit()
        ctx = build_context(eng)
        text = "{{.pass_pol}} for {{COMPANY_NAME}}"
        assert resolve_text(text, ctx) == text  # unchanged: neither token resolved
        assert resolve_text("{{COMPANY_NAME}}", ctx) == "Acme Corp"  # in isolation, it resolves fine


# --------------------------------------------------------------------------- lint


def test_lint_text_flags_unknown_key():
    assert lint_text("Hi {{COMPANY_NAME}}, see {{FOO_UNKNOWN}}.") == ["FOO_UNKNOWN"]
    assert lint_text("All known: {{COMPANY_NAME}} {{TARGET_HOST}}") == []


def test_lint_text_flags_foreign_token():
    assert lint_text("{{.pass_pol}}") == [".pass_pol"]


def test_lint_text_known_keys_param_suppresses_custom_vars():
    assert lint_text("{{APP_NAME}}") == ["APP_NAME"]
    assert lint_text("{{APP_NAME}}", known_keys={"APP_NAME"}) == []


def test_lint_doc_flags_unknown_key():
    doc = {
        "type": schema.DOC,
        "content": [
            {
                "type": schema.PARAGRAPH,
                "content": [
                    {"type": schema.TEXT, "text": "Host: "},
                    {"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}},
                    {"type": schema.TEXT, "text": " and also {{UNDEFINED_TOKEN}}."},
                    {"type": schema.VARIABLE, "attrs": {"key": "UNDEFINED_NODE_KEY"}},
                ],
            }
        ],
    }
    assert lint_doc(doc) == ["UNDEFINED_NODE_KEY", "UNDEFINED_TOKEN"]


def test_lint_doc_known_keys_from_db(session_factory):
    from fraction.templating import known_variable_keys

    with session_factory() as db:
        var = _custom_variable(db, "APP_NAME")
        db.add(var)
        db.commit()
        known = known_variable_keys(db)
        assert "APP_NAME" in known
        assert "COMPANY_NAME" in known  # builtins are defined TemplateVariable rows too

        doc = schema.doc_from_text("{{APP_NAME}} and {{STILL_UNKNOWN}}")
        assert lint_doc(doc, known) == ["STILL_UNKNOWN"]


# --------------------------------------------------------------------------- resolve_finding


def test_resolve_finding_combines_context_and_render(session_factory):
    block = {
        "type": schema.DOC,
        "content": [
            {
                "type": schema.PARAGRAPH,
                "content": [
                    {"type": schema.TEXT, "text": "Found on "},
                    {"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}},
                    {"type": schema.TEXT, "text": " at {{COMPANY_NAME}}."},
                ],
            }
        ],
    }
    with session_factory() as db:
        eng = _engagement(company_name="Acme Corp")
        finding = _finding(eng, target_host="10.0.0.9", content_json={"description": block})
        db.add(eng)
        db.add(finding)
        db.commit()

        blocks_html = resolve_finding(db, finding)
        assert "10.0.0.9" in blocks_html["description"]
        assert "Acme Corp" in blocks_html["description"]
        assert "{{" not in blocks_html["description"]


def test_resolve_finding_uses_custom_variable_overlay(session_factory):
    block = schema.doc_from_text("Env: {{ENV_LABEL}}")
    with session_factory() as db:
        eng = _engagement()
        finding = _finding(eng, content_json={"description": block})
        db.add(eng)
        db.add(finding)
        db.flush()

        var = _custom_variable(db, "ENV_LABEL", scope=VariableScope.finding)
        db.add(VariableValue(variable=var, finding_id=finding.id, value="production"))
        db.commit()

        blocks_html = resolve_finding(db, finding)
        assert "production" in blocks_html["description"]


# --------------------------------------------------------------------------- /preview endpoint


def test_preview_endpoint_text_mode(preview_client, preview_session_factory):
    with preview_session_factory() as db:
        eng = _engagement(company_name="Acme Corp")
        db.add(eng)
        db.commit()
        eng_id = eng.id

    resp = preview_client.post(
        "/fraction/api/preview",
        json={"engagement_id": eng_id, "text": "Hello {{COMPANY_NAME}}, ref {{FOO_UNKNOWN}}"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["resolved"] == "Hello Acme Corp, ref {{FOO_UNKNOWN}}"
    assert data["warnings"] == ["FOO_UNKNOWN"]


def test_preview_endpoint_doc_mode(preview_client, preview_session_factory):
    with preview_session_factory() as db:
        eng = _engagement(company_name="Acme Corp")
        db.add(eng)
        db.commit()
        eng_id = eng.id

    doc = schema.doc_from_text("Company is {{COMPANY_NAME}}")
    resp = preview_client.post("/fraction/api/preview", json={"engagement_id": eng_id, "doc": doc})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "Acme Corp" in data["resolved"]
    assert data["warnings"] == []


def test_preview_endpoint_finding_mode(preview_client, preview_session_factory):
    block = schema.doc_from_text("Host {{TARGET_HOST}} at {{COMPANY_NAME}}, see {{ODD_ONE}}")
    with preview_session_factory() as db:
        eng = _engagement(company_name="Acme Corp")
        finding = _finding(eng, target_host="10.0.0.7", content_json={"description": block})
        db.add(eng)
        db.add(finding)
        db.commit()
        eng_id, finding_id = eng.id, finding.id

    resp = preview_client.post(
        "/fraction/api/preview", json={"engagement_id": eng_id, "finding_id": finding_id}
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "10.0.0.7" in data["resolved"]["description"]
    assert "Acme Corp" in data["resolved"]["description"]
    assert data["warnings"] == ["ODD_ONE"]


def test_preview_endpoint_missing_engagement(preview_client):
    resp = preview_client.post("/fraction/api/preview", json={"engagement_id": 999999, "text": "hi"})
    assert resp.status_code == 404


def test_preview_endpoint_requires_engagement_id(preview_client):
    resp = preview_client.post("/fraction/api/preview", json={"text": "hi"})
    assert resp.status_code == 400


def test_preview_endpoint_requires_some_input(preview_client, preview_session_factory):
    with preview_session_factory() as db:
        eng = _engagement()
        db.add(eng)
        db.commit()
        eng_id = eng.id

    resp = preview_client.post("/fraction/api/preview", json={"engagement_id": eng_id})
    assert resp.status_code == 400


def test_builtin_keys_importable_from_package_root():
    # BUILTIN_KEYS is the frozen contract constant other modules (seed/loader.py, reporting/context.py)
    # import from `fraction.templating` -- confirm WS6's __init__.py edits didn't shadow/break it.
    assert set(BUILTIN_KEYS) == {
        "COMPANY_NAME",
        "ENGAGEMENT_NAME",
        "TARGET_HOST",
        "TARGET_PORT",
        "TARGET_URL",
        "ASSESSOR",
        "TODAY",
        "START_DATE",
        "END_DATE",
        "SEVERITY",
    }
