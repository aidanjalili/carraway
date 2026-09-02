"""Encrypting the history before it ever leaves this machine.

Carraway's server used to hold nothing worth stealing: category names and a
few figures. Once it holds transaction history that is no longer true, and
"it is my own box" is not a security model -- a $6 droplet with a public DNS
name is exactly the thing that gets owned while nobody is watching.

So the history is encrypted here, with a key the server never sees, and the
server stores an opaque blob it cannot read. Someone who takes the whole
database gets ciphertext.

The shape of it:

* A **vault key** is generated once on the laptop: 130 bits of randomness,
  written in the same unambiguous alphabet as the pairing code so it can be
  read off one screen and typed into another. It lives in the system keyring.
* That key is stretched into a 256-bit AES key with PBKDF2-HMAC-SHA256 over a
  random salt. The salt is stored beside the ciphertext, which is fine -- a
  salt is not a secret, it exists to stop one attack being reused across many
  vaults.
* The payload is sealed with **AES-256-GCM**, a fresh 12-byte nonce every
  time. GCM authenticates as well as encrypts, so a server that tampers with
  a byte produces a decryption failure rather than a plausible lie.

Two deliberate choices worth stating.

The primitives are AES-GCM and PBKDF2 rather than anything better, because
the other end of this is a browser and those are what the Web Crypto API
offers everywhere. scrypt would resist a GPU far better and is not available
there; a JavaScript implementation would mean shipping crypto code of our
own, which is a worse trade than using the primitive the platform has
audited.

And nothing here is hand-rolled. `cryptography` does the sealing. The one
thing this module must never become is somebody's own cipher.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass

# The same alphabet as a pairing code: no I, L, O or U, because a vault key
# is read off a laptop and typed into a phone and those four are a coin flip
# against the digits they resemble.
KEY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 25 characters of a 32-letter alphabet is 125 bits: well past anything that
# can be searched, short enough to type once, and it divides into five even
# groups of five rather than leaving a stray pair at the end.
KEY_LENGTH = 25
KEY_GROUP = 5

# OWASP's floor for PBKDF2-HMAC-SHA256. About half a second on a phone, paid
# once when the key is entered and then cached, so it costs the user nothing
# per read while costing an attacker every guess.
ITERATIONS = 600_000

SALT_BYTES = 16
NONCE_BYTES = 12  # what GCM is specified for; longer is not better here


class VaultError(RuntimeError):
    """Something went wrong sealing or opening the vault, phrased for a person."""


def new_key() -> str:
    """Mint a vault key. Shown once, typed into the phone once, then kept."""
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def format_key(key: str) -> str:
    """Grouped, so it can be read off a screen a chunk at a time."""
    return "-".join(key[index : index + KEY_GROUP] for index in range(0, len(key), KEY_GROUP))


def normalise_key(raw: str) -> str:
    """What someone typed, turned back into what was generated.

    Case folded, separators dropped, and the four look-alike letters folded
    onto the digits they resemble. None of those four is in the alphabet, so
    nothing genuine is ever folded onto something else.
    """
    folded = raw.strip().upper().replace("O", "0").replace("I", "1").replace("L", "1")
    return "".join(character for character in folded if character in KEY_ALPHABET)


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise VaultError(
            "Encrypting your history needs the 'cryptography' package. "
            "Install Carraway with the [pocket] extra."
        ) from exc
    return AESGCM(key)


def derive(vault_key: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    """Stretch the vault key into the 256-bit key that does the sealing."""
    import hashlib

    cleaned = normalise_key(vault_key)
    if not cleaned:
        raise VaultError("That does not look like a vault key.")
    return hashlib.pbkdf2_hmac("sha256", cleaned.encode("ascii"), salt, iterations, dklen=32)


@dataclass(frozen=True, slots=True)
class Sealed:
    """An encrypted payload and everything needed to open it but the key."""

    salt: str
    nonce: str
    ciphertext: str
    iterations: int = ITERATIONS
    algorithm: str = "AES-256-GCM"
    kdf: str = "PBKDF2-HMAC-SHA256"

    def as_json(self) -> dict:
        return {
            "salt": self.salt,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
            "iterations": self.iterations,
            "algorithm": self.algorithm,
            "kdf": self.kdf,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Sealed:
        return cls(
            salt=str(payload["salt"]),
            nonce=str(payload["nonce"]),
            ciphertext=str(payload["ciphertext"]),
            iterations=int(payload.get("iterations", ITERATIONS)),
            algorithm=str(payload.get("algorithm", "AES-256-GCM")),
            kdf=str(payload.get("kdf", "PBKDF2-HMAC-SHA256")),
        )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise VaultError("The stored vault is not readable.") from exc


def seal(payload: dict, vault_key: str, iterations: int = ITERATIONS) -> Sealed:
    """Encrypt `payload` so only a holder of the vault key can read it.

    A fresh salt and nonce every time. Reusing a nonce under one key is the
    way AES-GCM fails catastrophically rather than gracefully, so they are
    never derived from anything -- always drawn from the OS.
    """
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = derive(vault_key, salt, iterations)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = _aesgcm(key).encrypt(nonce, raw, None)
    return Sealed(
        salt=_b64(salt), nonce=_b64(nonce), ciphertext=_b64(ciphertext), iterations=iterations
    )


def open_sealed(sealed: Sealed, vault_key: str) -> dict:
    """Decrypt, or raise. A wrong key and a tampered blob fail the same way.

    GCM authenticates, so this cannot return a plausible-looking lie: either
    the whole payload is exactly what was sealed, or it raises.
    """
    key = derive(vault_key, _unb64(sealed.salt), sealed.iterations)
    try:
        raw = _aesgcm(key).decrypt(_unb64(sealed.nonce), _unb64(sealed.ciphertext), None)
    except Exception as exc:
        raise VaultError(
            "That vault key does not open this history — or the stored copy has been altered."
        ) from exc
    return json.loads(raw.decode("utf-8"))
