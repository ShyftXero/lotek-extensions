"""Canonical rich-text content = ProseMirror/TipTap JSON, rendered deterministically to HTML and docx.

``schema`` defines the doc shape + custom nodes; ``render_html`` is the JSON->HTML walker. The docx
walker (``render_docx``) is added by WS8 and shares the same node-dispatch approach.
"""

from scribble.content import schema  # noqa: F401
