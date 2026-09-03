"""``lotek_kit.flask_assets`` — putting package data on a URL.

The behaviours pinned here are the ones that bite at startup or in a template, where a mistake is a
boot failure or a silent 404 with dead drag-and-drop and no server error.
"""

from __future__ import annotations

import pytest

flask = pytest.importorskip("flask", reason="the flask extra is not installed")

from lotek_kit.flask_assets import (  # noqa: E402  (must follow the importorskip)
    BLUEPRINT_NAME,
    DEFAULT_URL_PREFIX,
    ensure_registered,
    registered_prefix,
)


@pytest.fixture
def app():
    return flask.Flask(__name__)


def test_an_unregistered_app_reports_no_prefix(app):
    assert registered_prefix(app) is None


def test_registering_puts_the_assets_on_the_requested_prefix(app):
    assert ensure_registered(app, "/scribble/kit") == "/scribble/kit"
    assert registered_prefix(app) == "/scribble/kit"


def test_the_default_prefix_is_used_when_none_is_given(app):
    assert ensure_registered(app) == DEFAULT_URL_PREFIX


def test_registering_twice_is_a_no_op_rather_than_an_exception(app):
    """Flask raises on a duplicate blueprint name and has no unregister. With core and one or more
    extensions all calling this at startup, a raise here would take the app down at boot."""
    first = ensure_registered(app, "/_kit")
    second = ensure_registered(app, "/_kit")
    assert first == second == "/_kit"


def test_a_second_caller_with_a_different_prefix_gets_the_first_one_back(app):
    """First caller wins, and the loser is TOLD it lost — returning the effective prefix rather than
    the requested one is what lets a caller building URLs by hand notice."""
    ensure_registered(app, "/_kit")
    assert ensure_registered(app, "/somewhere/else") == "/_kit"
    assert registered_prefix(app) == "/_kit"


def test_the_prefix_is_read_from_the_url_map_not_the_blueprint_attribute(app):
    """``app.blueprints[name].url_prefix`` is None for a blueprint whose prefix was supplied at
    registration time — which is every caller here. Reading it would report 'no prefix' for a
    perfectly well-registered kit."""
    ensure_registered(app, "/_kit")
    assert app.blueprints[BLUEPRINT_NAME].url_prefix is None
    assert registered_prefix(app) == "/_kit"


def test_url_for_resolves_the_same_expression_under_any_prefix(app):
    """The template-level contract: markup writes one expression and never learns where the kit is
    mounted."""
    ensure_registered(app, "/scribble/kit")
    with app.test_request_context():
        assert flask.url_for("lotek_kit.static", filename="reorder.js") == "/scribble/kit/reorder.js"


def test_the_served_path_has_no_extra_static_segment(app):
    ensure_registered(app, "/_kit")
    client = app.test_client()
    assert client.get("/_kit/reorder.js").status_code == 200
    assert client.get("/_kit/static/reorder.js").status_code == 404


@pytest.mark.parametrize("filename", ["reorder.js", "reorder.css"])
def test_every_shipped_asset_is_actually_reachable(app, filename):
    """The tripwire for a renamed asset: without it, a rename is a 404 in the browser, dead
    drag-and-drop, and nothing at all in the server log."""
    ensure_registered(app, "/_kit")
    response = app.test_client().get(f"/_kit/{filename}")
    assert response.status_code == 200
    assert response.get_data()


def test_traversal_out_of_the_asset_directory_is_refused(app):
    ensure_registered(app, "/_kit")
    client = app.test_client()
    for attempt in ("/_kit/../pyproject.toml", "/_kit/%2e%2e/pyproject.toml", "/_kit/../../etc/passwd"):
        assert client.get(attempt).status_code in (301, 308, 404)
