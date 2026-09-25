"""Gotenberg auto-provisioning: enabling scribble makes PDF work with zero setup. ensure_pdf_service()
prefers an operator's external SCRIBBLE_PDF_SERVICE_URL, else asks the host to bring up the managed
container (idempotently), and is a no-op once configured or when there is no host seam.
"""
from __future__ import annotations

import pytest

from scribble.reporting import exporters as EX


@pytest.fixture(autouse=True)
def _reset_pdf_config(monkeypatch):
    monkeypatch.delenv("SCRIBBLE_PDF_SERVICE_URL", raising=False)
    monkeypatch.delenv("SCRIBBLE_PDF_SERVICE_TOKEN", raising=False)
    EX.configure_gotenberg(None)
    yield
    EX.configure_gotenberg(None)


def _hook_returning(ensure, calls):
    def _hook(name):
        if name == "ensure_service_container":
            return ensure
        return None
    return _hook


def test_external_url_env_wins_no_container(monkeypatch):
    monkeypatch.setenv("SCRIBBLE_PDF_SERVICE_URL", "http://gotenberg.internal:3000")
    monkeypatch.setenv("SCRIBBLE_PDF_SERVICE_TOKEN", "tok")
    called = []
    monkeypatch.setattr("scribble.host.host_hook",
                        _hook_returning(lambda *a, **k: called.append(a) or {}, called))
    EX.ensure_pdf_service()
    assert EX.gotenberg_url() == "http://gotenberg.internal:3000"
    assert not called  # an external URL means we never provision a container


def test_managed_container_is_provisioned_and_configured(monkeypatch):
    calls = []

    def ensure(name, image, port, **kw):
        calls.append((name, image, port))
        return {"status": "created", "url": "http://127.0.0.1:3011", "container": "lotek-svc-gotenberg-x"}

    monkeypatch.setattr("scribble.host.host_hook", _hook_returning(ensure, calls))
    EX.ensure_pdf_service()
    assert EX.gotenberg_url() == "http://127.0.0.1:3011"
    assert calls == [("gotenberg", EX.GOTENBERG_IMAGE, EX.GOTENBERG_PORT)]


def test_no_host_seam_leaves_unconfigured(monkeypatch):
    monkeypatch.setattr("scribble.host.host_hook", lambda name: None)
    EX.ensure_pdf_service()
    assert EX.gotenberg_url() is None  # standalone: unconfigured, a later PDF 503s rather than crashing


def test_already_configured_is_a_noop(monkeypatch):
    EX.configure_gotenberg("http://preset:3000")
    calls = []
    monkeypatch.setattr("scribble.host.host_hook",
                        _hook_returning(lambda *a, **k: calls.append(a) or {"url": "x"}, calls))
    EX.ensure_pdf_service()
    assert EX.gotenberg_url() == "http://preset:3000"
    assert not calls  # memoized: no re-provision once a URL is set
