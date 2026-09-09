"""The per-user report-Theme preference — ``deps.host_user_setting`` and the precedence that reads it.

``[[user_settings]]`` is the host-owned per-USER seam (no admin gate), as distinct from ``[[settings]]``
and from ``ScribbleSettings.default_report_theme``, which is install-wide and admin-gated. Scribble
declares exactly one: ``preferred_report_theme``, consumed by
``report_html_api._selected_theme``.

**The precedence is the design, not an implementation detail**, so it is tested directly:

    ?theme=  →  the reader's own preference  →  this install's default

* BELOW ``?theme=`` so a report link you SEND renders the Theme that link names rather than the
  recipient's taste — a shared URL has to mean the same thing to everybody who opens it.
* ABOVE the install default so choosing a Theme actually takes effect for the person who chose it.
* Absent for an unauthenticated reader (a share link) or a PAT, because the HOST's reader resolves the
  session user and answers the default when there is not one. Nothing in scribble checks for that,
  which is the point — it cannot be forgotten here.

An adversarial review noted this behaviour shipped with no executing assertion on the extension side
(the paired lotek test skips until lotek re-pins scribble), which is what this file closes. Hermetic:
the host hook is a fake injected through ``extras``; no host, no database, no app.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from flask import Flask

from scribble import deps
from scribble.report_html_api import _selected_theme

MANIFEST = Path(__file__).resolve().parent.parent / "lotek-extension.toml"
DATA = tomllib.loads(MANIFEST.read_text())
USER_SETTINGS = {entry["key"]: entry for entry in DATA.get("user_settings", [])}
PREF_KEY = "preferred_report_theme"


class _Cfg:
    def __init__(self, hook=None):
        self.extras = {} if hook is None else {"extension_user_setting": hook}


@pytest.fixture()
def hosted(monkeypatch):
    """A fake host per-user reader; the returned dict sets what it answers with."""
    box: dict = {}

    def hook(key, default=None):
        if box.get("__raises__"):
            raise RuntimeError("the host hook blew up")
        return box.get(key, default)

    monkeypatch.setattr(deps, "get_config", lambda: _Cfg(hook))
    return box


@pytest.fixture()
def flask_app():
    """A BARE Flask app, only for a request context.

    Deliberately NOT named `app`: this package's conftest has an autouse fixture keyed on the literal
    fixture name `app` which wires a mock host into `app.extensions["scribble"]`, so naming it that
    would make every test here KeyError on a bare app. These tests need a request context and nothing
    else — `_selected_theme` only reads `request.args`.
    """
    return Flask(__name__)


# ── the declaration ────────────────────────────────────────────────────────────────────────────


def test_the_preference_is_declared_and_is_not_a_secret():
    assert PREF_KEY in USER_SETTINGS, "scribble must declare the per-user theme preference"
    spec = USER_SETTINGS[PREF_KEY]
    assert spec["type"] == "str"
    # An empty default is what makes "follow the install default" expressible at all.
    assert spec["default"] == ""
    # `secret = true` is dropped by the host for user settings; declaring it would be a false promise.
    assert not spec.get("secret")


# ── the accessor ───────────────────────────────────────────────────────────────────────────────


def test_standalone_with_no_host_resolves_to_the_default(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _Cfg())
    assert deps.host_user_setting(PREF_KEY, "") == ""
    assert deps.host_user_setting(PREF_KEY, "fallback") == "fallback"


def test_a_saved_preference_is_returned(hosted):
    hosted[PREF_KEY] = "midnight"
    assert deps.host_user_setting(PREF_KEY, "") == "midnight"


def test_a_throwing_host_hook_degrades_to_the_default(hosted):
    """A preference lookup must never be the thing that breaks the report it decorates."""
    hosted["__raises__"] = True
    assert deps.host_user_setting(PREF_KEY, "") == ""


def test_a_none_from_the_host_reads_as_the_default(hosted):
    hosted[PREF_KEY] = None
    assert deps.host_user_setting(PREF_KEY, "") == ""


# ── the precedence ─────────────────────────────────────────────────────────────────────────────


def test_an_explicit_query_theme_beats_the_users_preference(flask_app, hosted):
    """A link you SEND must render the Theme it names, not the recipient's preference."""
    hosted[PREF_KEY] = "midnight"
    with flask_app.test_request_context("/?theme=daylight"):
        assert _selected_theme("install-default") == "daylight"


def test_the_users_preference_beats_the_install_default(flask_app, hosted):
    hosted[PREF_KEY] = "midnight"
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") == "midnight"


def test_an_unset_preference_falls_through_to_the_install_default(flask_app, hosted):
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") == "install-default"


def test_a_whitespace_only_preference_is_treated_as_unset(flask_app, hosted):
    """Otherwise a stored `"  "` would be truthy and resolve to a Theme named whitespace."""
    hosted[PREF_KEY] = "   "
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") == "install-default"


def test_a_non_string_preference_does_not_break_the_report(flask_app, hosted):
    """The host seam promises a lookup never breaks the render; `_selected_theme` coerces rather than
    trusting the type, so a nonsense stored row degrades instead of raising mid-report."""
    hosted[PREF_KEY] = 42
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") == "42"
    hosted[PREF_KEY] = ["midnight"]
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") is not None


def test_with_no_preference_and_no_install_default_nothing_is_selected(flask_app, hosted):
    """`None` is what lets the caller fall back to the bundled Theme registry."""
    with flask_app.test_request_context("/"):
        assert _selected_theme(None) is None


def test_an_unauthenticated_reader_gets_the_install_default(flask_app, monkeypatch):
    """A share link and a PAT request have no session user, so the HOST's reader answers the default —
    which is what keeps a client's copy of a report on the install Theme rather than an operator's.

    Modelled as the host hook returning the default (what `make_user_reader` does with no
    `g.current_user`), because that decision belongs to the host and scribble must not re-implement it.
    """
    monkeypatch.setattr(deps, "get_config", lambda: _Cfg(lambda key, default=None: default))
    with flask_app.test_request_context("/"):
        assert _selected_theme("install-default") == "install-default"
