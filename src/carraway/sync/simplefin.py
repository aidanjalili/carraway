"""Read bank accounts through SimpleFIN Bridge.

SimpleFIN is the one aggregator that suits an open source project: read-only,
about $15/year paid by the user directly, no business agreement, and an
openly published protocol. Carraway never sees a bank password — the user
authenticates at SimpleFIN and hands Carraway the resulting URL.

The flow, per https://www.simplefin.org/protocol.html:

1. The user creates a **setup token** at their SimpleFIN Bridge account. It is
   a base64-encoded URL and can be claimed exactly once.
2. Carraway decodes it and POSTs to that URL, receiving an **access URL** with
   HTTP Basic credentials embedded in it. The setup token is now spent.
3. Every later sync is a GET against that access URL.

The access URL is the long-lived secret and lives in the keyring, never in the
database — see sync.credentials.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .. import __version__
from ..analysis.recurring import normalise_merchant
from ..core.models import Account, AccountType, Transaction, assign_occurrences
from ..core.money import Money
from .base import SyncResult

# SimpleFIN says nothing about account type, so it is inferred from the name
# the institution reports. A wrong guess only affects presentation, and the
# user can correct it, whereas refusing to guess would leave everything
# looking like a chequing account.
# Cloudflare sits in front of SimpleFIN Bridge and rejects urllib's default
# User-Agent by signature, returning "error code: 1010" before the request ever
# reaches SimpleFIN. The symptom is a 403 that looks exactly like an
# already-claimed token, so identifying the app by name is not politeness here
# — without it nothing works at all.
_USER_AGENT = f"Carraway/{__version__} (+https://github.com/aidanjalili/carraway)"

# Cloudflare's edge-block codes. Seeing one means the request was stopped
# before SimpleFIN saw it, which is a completely different problem from
# anything SimpleFIN itself would report.
_CLOUDFLARE_CODES = ("1010", "1020", "1015", "1006", "1009")


def _is_edge_block(body: str) -> bool:
    lowered = body.lower()
    if "cloudflare" in lowered or "attention required" in lowered:
        return True
    return "error code:" in lowered and any(code in body for code in _CLOUDFLARE_CODES)


_TYPE_HINTS: list[tuple[tuple[str, ...], AccountType]] = [
    (("credit card", "credit ", "visa", "mastercard", "amex"), AccountType.CREDIT_CARD),
    (("savings", "save", "money market"), AccountType.SAVINGS),
    (("loan", "mortgage", "student"), AccountType.LOAN),
    (("invest", "brokerage", "ira", "401k", "roth"), AccountType.INVESTMENT),
    (("cash", "wallet"), AccountType.CASH),
]


class SimpleFinError(RuntimeError):
    """SimpleFIN rejected a request or returned something unusable."""


def decode_setup_token(setup_token: str) -> str:
    """Decode a setup token to its claim URL without spending it.

    Separated from claiming so a paste can be checked before the one-use token
    is consumed. Getting this wrong costs the user a trip back to SimpleFIN to
    generate another, so it is worth being able to look first.
    """
    token = "".join(setup_token.split())  # tolerate newlines from a wrapped paste
    if not token:
        raise SimpleFinError("No setup token given")

    try:
        # Bridge tokens are unpadded in the wild often enough to be worth
        # fixing up rather than rejecting; base64 needs length % 4 == 0.
        padded = token + "=" * (-len(token) % 4)
        claim_url = base64.b64decode(padded, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise SimpleFinError(
            f"That is not a valid SimpleFIN setup token ({len(token)} characters). "
            "It should be one long base64 string from SimpleFIN Bridge, with no "
            "spaces. If you pasted an access URL by mistake, that starts with "
            "'https://' and is not a setup token."
        ) from exc

    if not claim_url.lower().startswith("https://"):
        # The protocol requires TLS; a plaintext claim URL would send the
        # resulting credentials over the wire in the clear.
        raise SimpleFinError(
            f"The token decoded to something that is not an HTTPS URL: {claim_url[:60]!r}"
        )
    return claim_url


def claim_setup_token(setup_token: str) -> str:
    """Exchange a one-use setup token for a durable access URL.

    A 403 here usually means the token has already been claimed — including by
    an earlier attempt of your own, since every attempt that reaches SimpleFIN
    spends it. The protocol says to treat an unexpected 403 as a possible
    compromise, so the message says both.
    """
    claim_url = decode_setup_token(setup_token)

    request = urllib.request.Request(claim_url, data=b"", method="POST")
    request.add_header("Content-Length", "0")
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            access_url = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        detail, body = "", ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace").strip()
        if body:
            detail = f" Server said: {body[:200]}"
        if exc.code == 403 and _is_edge_block(body):
            raise SimpleFinError(
                "Blocked by Cloudflare before reaching SimpleFIN."
                f"{detail}\n\n"
                "Your token was not used and is still valid. This is a network "
                "or client problem rather than anything wrong with the token — "
                "if it persists, a VPN or proxy in the path is the usual cause."
            ) from exc
        if exc.code == 403:
            raise SimpleFinError(
                "SimpleFIN rejected this token as already claimed."
                f"{detail}\n\n"
                "Every attempt that reaches SimpleFIN spends the token, so an "
                "earlier try — even one that appeared to fail — will have used "
                "it up. Generate a fresh token and paste it whole.\n"
                "If you have not tried before, treat this token as compromised "
                "and revoke it at SimpleFIN Bridge."
            ) from exc
        raise SimpleFinError(
            f"SimpleFIN returned HTTP {exc.code} when claiming the token.{detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SimpleFinError(f"Could not reach SimpleFIN: {exc.reason}") from exc

    if not access_url.lower().startswith("https://"):
        raise SimpleFinError(
            f"SimpleFIN returned something that is not an HTTPS access URL: {access_url[:80]!r}"
        )
    return access_url


def _get(access_url: str, params: dict[str, str]) -> dict[str, Any]:
    """GET /accounts against the access URL.

    The credentials are embedded in the URL, so urllib carries them itself; no
    Authorization header is assembled here and none is logged.
    """
    base = access_url.rstrip("/")
    url = f"{base}/accounts"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url)
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            # parse_float=Decimal keeps balances and amounts exact; a float
            # would already have lost precision by the time we saw it.
            return json.loads(response.read().decode("utf-8"), parse_float=Decimal)
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 403 and _is_edge_block(body):
            raise SimpleFinError(
                f"Blocked by Cloudflare before reaching SimpleFIN: {body[:120]}"
            ) from exc
        if exc.code == 403:
            raise SimpleFinError(
                "SimpleFIN rejected the stored access URL. It may have been "
                "revoked; run 'carraway simplefin setup' with a new token."
            ) from exc
        if exc.code == 402:
            raise SimpleFinError("SimpleFIN says payment is required on your account") from exc
        raise SimpleFinError(f"SimpleFIN returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SimpleFinError(f"Could not reach SimpleFIN: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SimpleFinError("SimpleFIN returned a response that is not JSON") from exc


def _account_type(name: str) -> AccountType:
    lowered = name.lower()
    for needles, kind in _TYPE_HINTS:
        if any(needle in lowered for needle in needles):
            return kind
    return AccountType.CHECKING


def _to_account(node: dict[str, Any], local_id: str) -> Account:
    name = str(node.get("name") or "Account")
    org = node.get("org") if isinstance(node.get("org"), dict) else {}
    return Account(
        id=local_id,
        name=name,
        type=_account_type(name),
        institution=str(org.get("name") or node.get("conn_id") or ""),
        currency=str(node.get("currency") or "USD"),
        external_id=str(node.get("id") or ""),
    )


def _to_transaction(node: dict[str, Any], account_id: str, currency: str) -> Transaction | None:
    raw = node.get("amount")
    if raw is None:
        return None
    # SimpleFIN's sign convention already matches Carraway's: positive is a
    # deposit, negative is a withdrawal. Nothing to invert.
    amount = Money.parse(str(raw), currency)

    stamp = node.get("posted") or node.get("transacted_at") or 0
    try:
        when = datetime.fromtimestamp(int(stamp), tz=UTC).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None

    description = " ".join(str(node.get("description") or "").split()) or "(no description)"
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account_id,
        date=when,
        amount=amount,
        description=description,
        merchant=normalise_merchant(description),
        pending=bool(node.get("pending", False)),
    )


class SimpleFinProvider:
    """A `sync.Provider` backed by SimpleFIN Bridge."""

    name = "simplefin"

    def __init__(self, access_url: str, account_ids: dict[str, str] | None = None) -> None:
        self.access_url = access_url
        # Maps SimpleFIN's account id to the local one, so re-syncing lands in
        # the same account rather than creating a duplicate each time.
        self.account_ids = account_ids or {}

    def fetch(self, *, since: date | None = None, pending: bool = False) -> SyncResult:
        params: dict[str, str] = {}
        if since:
            params["start-date"] = str(
                int(datetime.combine(since, datetime.min.time()).timestamp())
            )
        if pending:
            params["pending"] = "1"

        payload = _get(self.access_url, params)
        result = SyncResult()

        for error in payload.get("errlist") or []:
            # A failing connection should not cost the user the accounts that
            # did sync, so these are reported rather than raised.
            if isinstance(error, dict) and error.get("msg"):
                result.warnings.append(str(error["msg"]))

        for node in payload.get("accounts") or []:
            if not isinstance(node, dict):
                continue
            external = str(node.get("id") or "")
            local_id = self.account_ids.get(external) or uuid.uuid4().hex[:12]
            self.account_ids[external] = local_id

            account = _to_account(node, local_id)
            result.accounts.append(account)

            converted = [
                tx
                for raw in (node.get("transactions") or [])
                if isinstance(raw, dict)
                and (tx := _to_transaction(raw, local_id, account.currency)) is not None
            ]
            assign_occurrences(converted)
            result.transactions.extend(converted)

        return result
