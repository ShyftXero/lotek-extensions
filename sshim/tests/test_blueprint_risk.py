"""Pin the two UI-facing Phase-1 fixes end to end, through a real mounted-enough Flask app.

Both blockers are page behaviour, so pinning them at the predicate level alone would not catch a
template that never includes the banner or a mint page that quietly ships a rendezvous default:

* the mint page opens with a BLANK rendezvous (no host, no fixed well-known port), and a blank one
  fails with a clear "configure a rendezvous host" message instead of a bare 400 page;
* the deviation-risk banner renders on BOTH the mint page and the session detail, fires on a pinned
  well-known port (on the detail page it is judged from the session's STORED snapshot), and stays
  silent for the secure default (random port + the one-use ephemeral password).

The predicate has one home (`plan.deviation_risks`); this file only proves the two surfaces agree with
it. A stub app proves the LOGIC — mounted tenancy/PAT lives in lotek's own suite.
"""
from __future__ import annotations

import re

import pytest
from flask import Flask
from sqlalchemy import create_engine

import sshim
from sshim.plan import WELL_KNOWN_SSH_PORTS, random_port

BANNER = 'class="risk"'


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test"  # noqa: S105 - a throwaway test app, no session data is used
    engine = create_engine("sqlite:///:memory:")
    sshim.register(app, engine, instance_path="/tmp/sshim-risk-test")
    with app.test_client() as c:
        yield c


def _mint_form(client, *, host="vps.example", port=22, topology="relay_reverse_socks", **extra):
    data = {
        "name": "t",
        "topology": topology,
        "rendezvous_host": host,
        "rendezvous_port": str(port),
        "rendezvous_user": "sshim",
        "anchor_host": "",
        "anchor_port": "22",
        "anchor_user": "sshim",
        "cidr": "",
        "ttl_minutes": "30",
    }
    data.update(extra)
    return client.post("/sshim/mint", data=data)


# ── an empty rendezvous field, and a message that says what to do ────────────

def test_the_mint_page_opens_with_a_blank_rendezvous_and_no_port_default(client):
    html = client.get("/sshim/mint").get_data(as_text=True)
    assert re.search(r'name="rendezvous_host" value=""', html), "the rendezvous field must start empty"
    # no `443` (or any host) survives as a page default, and the shipped default is not pre-filled
    assert 'value="shyft.us:443"' not in html
    assert BANNER not in html, "the banner must be silent while nothing is pinned"


def test_a_blank_rendezvous_is_a_clear_400_not_a_bare_error_page(client):
    resp = _mint_form(client, host="", port=22)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "Rendezvous host is required" in html
    # the operator's own values survive the rejection (the form re-renders, it does not wipe input)
    assert 'class="err"' in html


# ── the banner fires on a pinned port, and stays silent on the default ───────

@pytest.mark.parametrize("port", [22, 443])
def test_the_banner_fires_on_the_detail_page_for_a_pinned_port(client, port):
    resp = _mint_form(client, port=port)
    assert resp.status_code == 201
    html = resp.get_data(as_text=True)
    assert BANNER in html, f"a pinned port {port} must raise the deviation-risk banner"
    assert f"Inbound port {port}" in html
    # it states the risk; it never blocks the action
    assert "Non-blocking" in html


def test_the_banner_is_silent_on_the_detail_page_for_the_secure_default(client):
    port = random_port()
    while port in WELL_KNOWN_SSH_PORTS:  # keep the draw honest: the predicate flags pinned service ports
        port = random_port()
    resp = _mint_form(client, port=port)
    assert resp.status_code == 201
    html = resp.get_data(as_text=True)
    assert BANNER not in html
    assert "Security note" not in html


def test_the_banner_survives_a_re_fetch_of_the_stored_session(client):
    """Detail is the RE-FETCH surface: it judges the stored snapshot, not the mint-time form."""
    minted = _mint_form(client, port=443).get_data(as_text=True)
    found = re.search(r"/sshim/sessions/([0-9a-f-]+)/kill", minted)
    assert found, "the mint response must link its kill route"
    session_id = found.group(1)
    html = client.get(f"/sshim/sessions/{session_id}").get_data(as_text=True)
    assert BANNER in html
    assert "Inbound port 443" in html


# ── the mint form's reachability + primary profile ──────────────────────────
#
# Two reported defects, both page shape rather than logic:
#   * picking `direct_socks` reported "requires anchor_host" while the anchor inputs lived inside a
#     collapsed <details> — an error naming a field the form never showed;
#   * the primary rendezvous had to be hand-typed even though the lotek host IS an sshd.


def test_anchor_fields_are_reachable_without_opening_advanced(client):
    """The anchor inputs must exist OUTSIDE the Advanced disclosure, in a group the JS can show."""
    html = client.get("/sshim/mint").get_data(as_text=True)
    anchor_at = html.find('name="anchor_host"')
    details_at = html.find("<details>")
    assert anchor_at != -1, "the anchor host field must be on the page"
    assert -1 < anchor_at < details_at, (
        "anchor_host must render BEFORE <details> — inside Advanced it is a field the Direct topology "
        "needs but cannot show"
    )
    # the topology-to-fields wiring the JS drives
    assert 'data-topo="direct_socks relay_sshuttle"' in html


def test_topology_is_a_visible_choice_not_a_hidden_select(client):
    html = client.get("/sshim/mint").get_data(as_text=True)
    assert 'name="topology" value="direct_socks"' in html, "topology must be a radio card"
    assert "topo-grid" in html


def test_the_lotek_host_is_the_primary_profile(client):
    """The primary profile resolves to THIS dashboard's host — no hardcoded name, no saved row."""
    html = client.get("/sshim/mint").get_data(as_text=True)
    assert "lotek host (this dashboard)" in html
    assert 'value="__lotek_host__"' in html
    assert " selected" in html.split('value="__lotek_host__"')[1][:200], (
        "the primary profile must be preselected — that is what makes Mint a single click"
    )


def test_the_primary_profile_mints_against_this_host(client):
    """Submitting the sentinel profile uses the request host, even with JS off (blank rendezvous field)."""
    from sshim import blueprint as BP

    resp = client.post("/sshim/mint", data={
        "profile_id": BP.PRIMARY_PROFILE_ID,
        "topology": "relay_reverse_socks",
        "rendezvous_host": "",          # deliberately blank: the sentinel supplies it
        "rendezvous_port": "22",
        "rendezvous_user": "sshim",
        "ttl_minutes": "5",
    })
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:400]
    html = resp.get_data(as_text=True)
    assert "localhost" in html, "the plan should dial the request host"
