"""Assemble the self-contained deliverable HTML.

The deliverable is the whole point of Vector: one `.html` file that renders the interactive attack path
with **no external requests** — the viewer runtime (JS + CSS) and the model JSON are inlined. The same
``static/vector-viewer.{js,css}`` powers the in-editor live preview, so the deliverable can never drift
from what the author saw.

Security: the model is embedded inside a ``<script type="application/json">`` block via
:func:`json_for_script`, which neutralizes ``</script>``, HTML-comment, and JS line-separator breakouts —
the model is user-authored and could otherwise inject markup/script into the exported file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape
from markupsafe import Markup

from vector.schema import normalize

_PKG = Path(__file__).resolve().parent
_STATIC = _PKG / "static"
_TEMPLATE = _PKG / "templates" / "vector" / "deliverable.html.j2"

# Characters that must be escaped to embed JSON safely inside an HTML <script> element.
#   < > &  — so a "</script>" / "<!--" sequence in author text can't break out of the script.
#   U+2028 / U+2029 — valid in JSON strings but illegal JS line terminators (would be a syntax error).
_SCRIPT_ESCAPES = {
    ord("<"): "\\u003c",
    ord(">"): "\\u003e",
    ord("&"): "\\u0026",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}


def json_for_script(obj) -> str:
    """Serialize ``obj`` to JSON safe to embed inside an HTML ``<script>`` element."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).translate(_SCRIPT_ESCAPES)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _env_and_template():
    env = Environment(autoescape=select_autoescape(["html", "j2"]))
    return env, env.from_string(_read(_TEMPLATE))


def render_deliverable(model, *, title: str | None = None, footer: Any = "") -> str:
    """Return a complete, self-contained HTML document for ``model`` (any dict; normalized here).

    ``footer`` is typed ``Any``, not ``str``, because that is the truth: it arrives from the host's
    generic ``extras["extension_setting"]`` seam, which promises nothing about the type. Coerced and
    bounded here rather than trusted — see the call below.

    ``footer`` is the host-held ADMIN setting ``deliverable_footer`` (lotek#485) — a per-install line
    stamped under the diagram. Passed IN rather than resolved here so this stays a pure function with
    no app-context dependency: the three export call sites read it from
    ``deps.host_setting("deliverable_footer", "")``. Autoescaped like ``title``, because an operator
    typing HTML into an admin form must not inject markup into a client deliverable.
    """
    doc = normalize(model)
    css = _read(_STATIC / "vector-viewer.css")
    js = _read(_STATIC / "vector-viewer.js")
    _, template = _env_and_template()
    doc_title = title or (doc.get("meta", {}) or {}).get("title") or "Attack path"
    return template.render(
        title=doc_title,  # autoescaped
        css=Markup(css),
        js=Markup(js),
        model_json=Markup(json_for_script(doc)),
        # str() is not decoration: `host_setting` promises that a settings lookup never breaks the
        # export it decorates, but that promise died here — `extras["extension_setting"]` is a
        # GENERIC host seam, and a host returning a dict/int (a JSON-typed setting, a host that
        # namespaces differently) made `.strip()` raise AttributeError and 500 the client
        # deliverable, not the settings page. Truncated for the same reason: the bound belongs on
        # both sides of a seam the host owns.
        footer=str(footer or "").strip()[:200],  # autoescaped
    )
