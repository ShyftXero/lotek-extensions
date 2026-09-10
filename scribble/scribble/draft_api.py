"""AI draft API — "Draft with AI" for a finding's prose blocks (Phase 3 of the lotek AI engine).

Streams an AI-drafted continuation for one content block (description / remediation / details) via the
host-injected ``ai_stream`` hook (core ``app.ai.stream``, exposed as ``extras['ai_stream']``). The model's
text streams into a preview the operator reviews and copies into the editor — this route NEVER writes
``content_json`` (``autosave_api`` owns that), so the human stays in the loop and the draft is advisory.

Fails closed when the host injected no AI hook (extension mounted on a core without the hook, or AI
completion disabled): a 503 with a plain-text explanation, never a 500.

Same ``register(api_bp, bp)`` shape as ``autosave_api`` (this module does not touch the frozen
``api.py`` / ``blueprint.py`` / ``__init__.py`` internals — the driver calls ``register``). Auth is
inherited: the blueprint-wide tenancy gate (``scribble/authz.py``) resolves ``finding_id`` to its
engagement and applies the can-VIEW check fail-closed, and the host's request-method/role gate blocks a
read-only (viewer) POST — exactly the pair the write-bearing autosave route relies on. The master egress
gate is core's ``ai_completion_enabled`` (default OFF), enforced inside ``ai_stream`` itself.
"""
from __future__ import annotations

from collections.abc import Iterator

from flask import Response

from scribble import host
from scribble.deps import open_session
from scribble.models import EngagementFinding

_REGISTERED = False
_PLAIN = "text/plain; charset=utf-8"

#: Per-block intent, used only when a block is EMPTY (nothing to rephrase yet) — a first-pass draft.
_BLOCK_INTENT = {
    "description": "Write the DESCRIPTION: what the vulnerability is, where it occurs, and why it matters.",
    "remediation": "Write the REMEDIATION: concrete, actionable steps to fix this vulnerability.",
    "details": "Write the technical DETAILS: reproduction, evidence and mechanism for this finding.",
}
_REPHRASE_SYSTEM = (
    "You are a penetration-test report editor. Rephrase the provided section text to read more clearly and "
    "professionally, preserving its technical meaning and every fact — do not invent findings or add new "
    "claims. Output the rewritten prose only — no markdown headings, no preamble, no commentary."
)
_DRAFT_SYSTEM = (
    "You are a penetration-test report writer. Write concise, professional prose for one section of a "
    "security finding. Output prose only — no markdown headings, no preamble, no sign-off."
)


def _doc_to_text(doc) -> str:
    """Best-effort plain text from a ProseMirror ``doc`` (for prompt context). Never raises."""
    out: list[str] = []

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            out.append(node["text"])
        for child in node.get("content") or []:
            walk(child)

    walk(doc if isinstance(doc, dict) else {})
    return " ".join(out).strip()


def _build_messages(title: str, severity: str, block: str, current: str) -> list[dict]:
    header = f"Finding: {title or '(untitled)'} (severity: {severity or 'unknown'})\nSection: {block}"
    if current.strip():
        # The primary action: rephrase what the operator already wrote.
        return [
            {"role": "system", "content": _REPHRASE_SYSTEM},
            {"role": "user", "content": f"{header}\n\nRephrase this section:\n{current}"},
        ]
    # Nothing to rephrase yet — draft a first pass from the block's intent.
    intent = _BLOCK_INTENT.get(block, f"Write the '{block}' section for this finding.")
    return [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user", "content": f"{header}\n{intent}"},
    ]


def register(api_bp, bp) -> None:
    """Attach the AI-draft route onto the shared API blueprint. Idempotent (see ``autosave_api``)."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    @api_bp.post("/findings/<uuid:finding_id>/rephrase/<string:block>")
    def rephrase_block(finding_id, block: str):
        ai_stream = host.host_hook("ai_stream")
        if ai_stream is None:
            return Response(
                "[AI drafting is unavailable — enable AI completion in lotek settings]",
                status=503, mimetype=_PLAIN,
            )

        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None:
                return Response("[finding not found]", status=404, mimetype=_PLAIN)
            title = getattr(finding, "title", "") or ""
            sev = getattr(finding, "severity", None)
            severity = getattr(sev, "value", None) or (str(sev) if sev is not None else "")
            current = _doc_to_text((finding.content_json or {}).get(block))

        messages = _build_messages(title, str(severity), block, current)
        try:
            deltas = ai_stream(messages)  # eager gate + upstream connect happen host-side, in-context
        except Exception as exc:  # noqa: BLE001 — core's AiUnavailable type is not importable here
            return Response(f"[AI unavailable: {type(exc).__name__}]", status=503, mimetype=_PLAIN)

        def gen() -> Iterator[str]:
            got = False
            try:
                for delta in deltas:
                    got = True
                    yield delta
            except Exception as exc:  # noqa: BLE001 — surface a mid-stream drop inline, never a 500
                yield f"\n[AI stream interrupted: {type(exc).__name__}]"
            if not got:
                yield "[the model returned no content]"

        return Response(gen(), mimetype=_PLAIN)
