"""Seal one small secret at rest — the pivot password, so its OWNER can re-fetch it.

The re-fetch story needs the password back (mint on one box, borrow another, pull the commands and the
credential out again), so ``sshim_sessions.ssh_password`` holds CIPHERTEXT, not cleartext. sshim cannot
import core's ``EncryptedToken`` (an extension must not import ``app.*``), so this is a deliberately
tiny encrypt-then-MAC box with no new dependency:

* a 32-byte key, created 0600 in the instance dir on FIRST seal — never in the DB, never in a log,
  never in the package;
* HMAC-SHA256 as both the keystream and the authentication tag (derive-then-XOR, the standard
  CTR-from-a-PRF construction);
* a random 16-byte nonce per seal, so sealing the same password twice yields different ciphertext.

FAIL CLOSED. A missing, truncated or tampered key/token raises :class:`SecretUnavailable` — this module
never returns garbage, and the surfaces that call it (``service.reveal_password``) refuse the secret and
tell the operator to pin a key or re-mint, rather than printing nonsense into a terminal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

#: The key file's name inside the instance dir. Chmod 0600 on creation; the DB never sees it.
KEY_FILENAME = "secretbox.key"
KEY_BYTES = 32
_NONCE_BYTES = 16
_TAG_BYTES = 32
_VERSION = b"\x01"
_ENC_LABEL = b"sshim-secretbox-enc"
_MAC_LABEL = b"sshim-secretbox-mac"


class SecretUnavailable(Exception):
    """The stored secret cannot be opened (no key / corrupt key / tampered token). Never leaks a value."""


def key_path(instance_path) -> Path:
    """Where this instance's sealing key lives (a path — reading it is :func:`_load_key`'s job)."""
    return Path(instance_path) / KEY_FILENAME


def _load_key(path: Path, *, create: bool) -> bytes:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another request won the race; read the winner's key
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(KEY_BYTES))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretUnavailable("the sshim instance key is missing") from exc
    if len(raw) != KEY_BYTES:
        raise SecretUnavailable("the sshim instance key is corrupt")
    return raw


def _subkeys(key: bytes) -> tuple[bytes, bytes]:
    return (
        hmac.new(key, _ENC_LABEL, hashlib.sha256).digest(),
        hmac.new(key, _MAC_LABEL, hashlib.sha256).digest(),
    )


def _tag(mac_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()


def _xor_keystream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    for block in range(0, len(data), 32):
        stream = hmac.new(enc_key, nonce + block.to_bytes(4, "big"), hashlib.sha256).digest()
        chunk = data[block:block + 32]
        out[block:block + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, stream, strict=False))
    return bytes(out)


def seal(plaintext: str, *, key_path: Path) -> str:
    """Seal a secret for storage: URL-safe base64 of ``version | nonce | ciphertext | tag``."""
    enc_key, mac_key = _subkeys(_load_key(key_path, create=True))
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = _xor_keystream(enc_key, nonce, plaintext.encode("utf-8"))
    blob = _VERSION + nonce + ciphertext + _tag(mac_key, nonce, ciphertext)
    return base64.urlsafe_b64encode(blob).decode("ascii")


def unseal(token: str, *, key_path: Path) -> str:
    """Open a sealed secret. Raises :class:`SecretUnavailable` for a missing key, a corrupt key, or a
    token that fails its MAC — a caller must treat that as \"no secret\", never as a displayable value."""
    try:
        blob = base64.urlsafe_b64decode((token or "").encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretUnavailable("the stored secret is not readable") from exc
    if len(blob) < 1 + _NONCE_BYTES + _TAG_BYTES or blob[:1] != _VERSION:
        raise SecretUnavailable("the stored secret is not readable")
    nonce = blob[1:1 + _NONCE_BYTES]
    ciphertext = blob[1 + _NONCE_BYTES:-_TAG_BYTES]
    tag = blob[-_TAG_BYTES:]
    enc_key, mac_key = _subkeys(_load_key(key_path, create=False))
    if not hmac.compare_digest(tag, _tag(mac_key, nonce, ciphertext)):
        raise SecretUnavailable("the stored secret failed its integrity check")
    return _xor_keystream(enc_key, nonce, ciphertext).decode("utf-8")
