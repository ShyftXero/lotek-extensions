"""Regression for ext#120: an ``is_admin`` METHOD must not promote everyone to admin.

``bool(<bound method>)`` is ``True``. If a mounted host's ``User`` ever grows an ``is_admin()`` METHOD
(rather than a property/attribute), a ``bool(getattr(actor, "is_admin", False))`` read would silently
treat EVERY logged-in user as an admin — and in Vector that promotion widens visibility to every
diagram, including legacy NULL-owner rows. This pins the identity-compare fix in ``vector.deps`` —
mirrors ``bugreport/tests/test_authz.py::test_an_is_admin_METHOD_does_not_make_everyone_an_admin``.
"""

from __future__ import annotations

import uuid

from conftest import login

from vector.deps import current_actor_is_admin


class _MethodUser:
    def __init__(self) -> None:
        self.id = uuid.uuid7()
        self.username = "bob"
        self.role = None  # no role signal — forces the fall-through to the actor.is_admin read

    def is_admin(self):  # a METHOD, not a property — truthy as a bare attribute
        return False


def test_an_is_admin_method_does_not_make_everyone_an_admin(app):
    login(app, _MethodUser())
    with app.app_context():
        assert current_actor_is_admin() is False
