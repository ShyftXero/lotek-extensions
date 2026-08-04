"""Variable templating: build a context from an engagement/finding and resolve {{VARIABLES}}.

Resolution runs only at render time (stored content keeps raw ``{{ }}``). The names re-exported from
``resolver`` are a FROZEN CONTRACT (plans/CONTRACTS.md §5). WS6 additionally owns the DB-aware
custom-variable overlay (``context.py``) and the preview/lint helpers (``lint.py``), re-exported below.
"""

from fraction.templating.context import (  # noqa: F401
    build_full_context,
    known_variable_keys,
    load_variable_values,
)
from fraction.templating.lint import (  # noqa: F401
    lint_doc,
    lint_text,
    resolve_finding,
)
from fraction.templating.resolver import (  # noqa: F401
    BUILTIN_KEYS,
    build_context,
    make_var_resolver,
    resolve_doc,
    resolve_text,
)
