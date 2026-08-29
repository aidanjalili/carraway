"""Where a provider's secret lives.

A SimpleFIN access URL contains embedded HTTP Basic credentials, so it is a
password in every sense: anyone holding it can read the user's bank data until
it is revoked. It therefore never goes in the SQLite file, which users copy
between machines and back up casually.

Two stores, preferred in order:

1. **The system keyring** — Secret Service on Linux, Keychain on macOS. This
   is the right place for a secret and the only one that keeps it encrypted at
   rest. It needs the `keyring` package, which is an optional extra rather
   than a core dependency.
2. **A 0600 file** under XDG config. Weaker, because it is plaintext on disk,
   and the app says so plainly rather than implying a safety it does not have.

The fallback exists because a headless or minimal system often has no Secret
Service running at all, and refusing to work there would push people toward
pasting the token into a shell script instead — which is worse.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_SERVICE = "carraway"


def _fallback_path(key: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "carraway" / f"{key}.secret"


def _keyring():
    """The keyring module, or None when it is unavailable or unusable.

    An import success is not enough: a keyring with no backend raises only when
    actually used, so the caller must still handle failure at call time.
    """
    try:
        import keyring

        return keyring
    except ImportError:
        return None


def store(key: str, secret: str) -> str:
    """Save a secret. Returns the name of the store that accepted it."""
    ring = _keyring()
    if ring is not None:
        try:
            ring.set_password(_SERVICE, key, secret)
            return "system keyring"
        except Exception:
            # Any keyring backend failure (no Secret Service, locked keyring,
            # D-Bus missing) falls through to the file, rather than losing the
            # credential the user just pasted.
            pass

    path = _fallback_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start; writing first and
    # chmod-ing after leaves a window where the secret is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return f"{path} (plain text, readable only by you)"


def load(key: str) -> str | None:
    """Read a secret back, or None if it was never stored."""
    ring = _keyring()
    if ring is not None:
        try:
            secret = ring.get_password(_SERVICE, key)
            if secret:
                return secret
        except Exception:
            pass

    path = _fallback_path(key)
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def delete(key: str) -> bool:
    """Remove a secret from every store. True if anything was removed."""
    removed = False
    ring = _keyring()
    if ring is not None:
        try:
            if ring.get_password(_SERVICE, key):
                ring.delete_password(_SERVICE, key)
                removed = True
        except Exception:
            pass

    path = _fallback_path(key)
    if path.exists():
        path.unlink()
        removed = True
    return removed


def describe_store() -> str:
    """Which store new secrets will go to, for the user to see before saving."""
    ring = _keyring()
    if ring is not None:
        try:
            backend = ring.get_keyring()
            name = type(backend).__name__
            if "fail" not in name.lower():
                return f"system keyring ({name})"
        except Exception:
            pass
    return "a 0600 file under ~/.config/carraway (no system keyring available)"
