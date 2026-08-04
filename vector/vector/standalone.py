"""Minimal standalone Flask app — run Vector on its own SQLite DB, no host required.

    python -m vector            # serves http://127.0.0.1:5099/vector
    VECTOR_PORT=8080 python -m vector

The same package mounts into a host (lotek) via :func:`vector.register`; this module is only the
standalone harness (its own engine + base template + CSRF), mirroring Fraction's standalone app.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, redirect
from sqlalchemy import create_engine

from vector import register


def create_app(*, db_path: str | Path | None = None, instance_path: str | Path | None = None,
               testing: bool = False, seed: bool = True) -> Flask:
    inst = Path(instance_path or os.environ.get("VECTOR_INSTANCE") or (Path.cwd() / "instance"))
    inst.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__, instance_path=str(inst))
    app.config["SECRET_KEY"] = os.environ.get("VECTOR_SECRET_KEY", "vector-dev-secret-not-for-prod")
    app.config["TESTING"] = testing
    if testing:
        app.config["WTF_CSRF_ENABLED"] = False

    # CSRF protection for the cookie-authed browser API (the editor sends X-CSRFToken). Optional import
    # so a stripped environment without flask-wtf still boots read-only.
    if not testing:
        try:
            from flask_wtf import CSRFProtect

            CSRFProtect(app)
        except Exception:  # noqa: BLE001 - CSRF is a hardening layer, never a boot blocker standalone
            pass

    db = db_path or (inst / "vector.sqlite")
    engine = create_engine(f"sqlite:///{db}", future=True)
    register(app, engine, base_template="vector/base.html", instance_path=inst)

    if seed:
        from vector.db import make_session_factory
        from vector.seed import seed_defaults

        with make_session_factory(engine)() as session:
            seed_defaults(session)
            session.commit()

    @app.get("/")
    def _index():
        return redirect("/vector")

    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("VECTOR_PORT", "5099"))
    host = os.environ.get("VECTOR_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=bool(os.environ.get("VECTOR_DEBUG")))


if __name__ == "__main__":
    main()
