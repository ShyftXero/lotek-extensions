"""Custom-variable overlay: load ``VariableValue`` rows and merge them onto the built-in context.

``resolver.build_context`` computes the built-in context (``COMPANY_NAME``, ``TARGET_HOST``, ...)
structurally from ``Engagement``/``EngagementFinding`` attributes and never touches the database. This
module is the DB-aware layer WS6 adds on top of it: it loads user-defined ``TemplateVariable`` /
``VariableValue`` rows (FACTION's ``CustomType``/``CustomField``) and overlays them.

**Precedence (lowest -> highest):**

1. Built-in structural context (``resolver.BUILTIN_KEYS``, computed from engagement/finding attributes).
2. Engagement-scope custom variable values (``VariableValue.engagement_id`` set, ``finding_id`` unset).
3. Finding-scope custom variable values (``VariableValue.finding_id`` set) -- overrides an
   engagement-scope value of the same key.
4. ``extra`` passed explicitly by the caller of :func:`build_full_context` (e.g. a preview request
   testing a one-off override).

A custom variable can never *silently* shadow a built-in key: :func:`load_variable_values` only loads
rows bound to a ``TemplateVariable`` with ``builtin=False``. ``TemplateVariable.key`` is unique, so a
custom variable cannot even be *created* under a name already claimed by a seeded builtin row today --
this filter is kept anyway as an explicit, defensive statement of intent (it also guards against future
data issues, e.g. a migration that clones a builtin definition). If you deliberately want to override a
built-in's *value* for one render -- without touching the underlying engagement/finding field -- pass it
via ``extra``; that is the sanctioned, explicit override path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from fraction.templating.resolver import build_context


def load_variable_values(session, engagement, finding=None) -> dict[str, str]:
    """Load custom (non-builtin) ``VariableValue`` values for ``engagement`` (and ``finding``, if given),
    keyed by ``TemplateVariable.key``. Finding-scope values win over engagement-scope values of the same
    key. Rows with a ``NULL`` value are skipped (treated as "not set", falling through to the built-in
    or a lower-precedence value)."""
    from fraction.models import TemplateVariable, VariableValue

    eng_stmt = (
        select(TemplateVariable.key, VariableValue.value)
        .join(VariableValue, VariableValue.variable_id == TemplateVariable.id)
        .where(
            VariableValue.engagement_id == engagement.id,
            VariableValue.finding_id.is_(None),
            TemplateVariable.builtin.is_(False),
        )
    )
    values: dict[str, str] = {key: value for key, value in session.execute(eng_stmt) if value is not None}

    if finding is not None:
        finding_stmt = (
            select(TemplateVariable.key, VariableValue.value)
            .join(VariableValue, VariableValue.variable_id == TemplateVariable.id)
            .where(
                VariableValue.finding_id == finding.id,
                TemplateVariable.builtin.is_(False),
            )
        )
        values.update(
            {key: value for key, value in session.execute(finding_stmt) if value is not None}
        )

    return values


def build_full_context(
    session, engagement, finding=None, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The complete ``{{VARIABLE}}`` context: built-ins overlaid with custom ``VariableValue`` rows,
    overlaid with ``extra``. See the module precedence order above. This is the context most render/
    preview call sites should use (``resolver.build_context`` alone only covers built-ins)."""
    overlay = load_variable_values(session, engagement, finding)
    if extra:
        overlay = {**overlay, **extra}
    return build_context(engagement, finding, extra=overlay)


def known_variable_keys(session) -> set[str]:
    """All defined ``TemplateVariable.key`` values (builtin + custom). Used to make ``lint_text``/
    ``lint_doc`` DB-aware -- a token matching a defined custom variable is not "unknown" even though it
    isn't in ``BUILTIN_KEYS``."""
    from fraction.models import TemplateVariable

    return set(session.scalars(select(TemplateVariable.key)))
