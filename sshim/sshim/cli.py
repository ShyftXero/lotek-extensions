"""Operator entry points for the one thing a request-throttled sweep cannot guarantee: a forced reap.

The browser blueprint sweeps at most once per ``service.SWEEP_INTERVAL_SECONDS`` and only while someone
is loading a page — a quiet instance, or one whose traffic lands on a different worker, would notice a
kill date late. This command makes the reap explicit, so a cron job (or a human) can force it:

    flask sshim-reap

Registered on ``app.cli`` by ``sshim.register``. It is idempotent (``service.sweep_expired``), so running
it alongside the ``before_request`` sweep is safe: the second call simply has nothing left to kill.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext

from sshim.deps import get_config
from sshim.service import sweep_expired


@click.command("sshim-reap")
@with_appcontext
def reap_command() -> None:
    """Mark every sshim session past its kill date as terminal (and report TTL-expired ones)."""
    try:
        cfg = get_config()
    except RuntimeError as exc:  # pragma: no cover - misconfiguration guard
        raise click.ClickException(str(exc)) from exc
    with cfg.session_factory() as db:
        result = sweep_expired(db)
    click.echo(f"sshim reap: killed={result['killed']} expired={result['expired']}")
