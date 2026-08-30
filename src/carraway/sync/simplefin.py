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
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .. import __version__
from ..analysis.recurring import normalise_merchant
from ..core.models import Account, Transaction, assign_occurrences
from ..core.money import Money
from .accounts import classify_account
from .base import SyncResult
from .net import urlopen

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


# SimpleFIN corrupts some institution names before we ever see them — a
# registered-trademark sign arrives as literal U+FFFD replacement characters.
# Stripping them beats showing "VISA�� CARD" in the account list.
_REPLACEMENT_RUN = re.compile("�+")


def clean_name(name: str) -> str:
    """Tidy an institution-supplied name for display.

    >>> clean_name("WELLS FARGO ACTIVE CASH VISA\ufffd\ufffd CARD ...5309 (5309)")
    'WELLS FARGO ACTIVE CASH VISA CARD ...5309 (5309)'
    """
    return re.sub(r"\s{2,}", " ", _REPLACEMENT_RUN.sub("", name)).strip()


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
        with urlopen(request, timeout=30) as response:
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


def split_credentials(access_url: str) -> tuple[str, str]:
    """Separate an access URL into (url without credentials, Basic auth value).

    SimpleFIN hands back `https://user:pass@host/simplefin`. urllib cannot use
    that form — it parses the password as a port and raises — so the userinfo
    is stripped out and sent as an Authorization header instead. Keeping this
    in one place also means the credential never ends up in a URL that could
    be logged or printed in a traceback.
    """
    parsed = urllib.parse.urlsplit(access_url)
    if not parsed.username:
        return access_url, ""

    user = urllib.parse.unquote(parsed.username)
    password = urllib.parse.unquote(parsed.password or "")
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"

    clean = urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return clean, f"Basic {token}"


def _get(access_url: str, params: dict[str, str]) -> dict[str, Any]:
    """GET /accounts against the access URL."""
    base, authorization = split_credentials(access_url)
    url = base.rstrip("/") + "/accounts"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(url)
    request.add_header("User-Agent", _USER_AGENT)
    if authorization:
        request.add_header("Authorization", authorization)

    try:
        with urlopen(request, timeout=60) as response:
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
        # A 4xx usually carries a JSON explanation worth far more than the
        # status code — "No connections available." tells the user exactly
        # what to do, where "HTTP 400" tells them nothing.
        for message in _messages_in(body):
            raise SimpleFinError(message) from exc
        raise SimpleFinError(f"SimpleFIN returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        # Deliberately reports the reason only. The URL carries credentials,
        # and urllib puts it in the exception text.
        raise SimpleFinError(f"Could not reach SimpleFIN: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SimpleFinError("SimpleFIN returned a response that is not JSON") from exc


def _messages_in(body: str) -> list[str]:
    """Human-readable errors out of a SimpleFIN response body.

    The published protocol calls the field `errlist` with `{code, msg}`
    objects, but the live Bridge returns `errors` holding plain strings. Both
    are read, because a response that explains itself should not be discarded
    over a field name.
    """
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    messages: list[str] = []
    for entry in (payload.get("errors") or []) + (payload.get("errlist") or []):
        if isinstance(entry, str) and entry.strip():
            messages.append(entry.strip())
        elif isinstance(entry, dict) and entry.get("msg"):
            messages.append(str(entry["msg"]))
    return messages


def _to_account(node: dict[str, Any], local_id: str) -> Account:
    name = clean_name(str(node.get("name") or "Account")) or "Account"
    org = node.get("org") if isinstance(node.get("org"), dict) else {}
    return Account(
        id=local_id,
        name=name,
        type=classify_account(name, node.get("balance")),
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
        # How many HTTP requests this provider has made. SimpleFIN allows 24 a
        # day and one full sync spends about six, so the caller has to be able
        # to measure rather than guess.
        self.requests_made = 0
        # Maps SimpleFIN's account id to the local one, so re-syncing lands in
        # the same account rather than creating a duplicate each time.
        self.account_ids = account_ids or {}

    # SimpleFIN returns accounts but *no transactions at all* when no
    # start-date is given — it only volunteers yesterday onward — which reads
    # as "this account is empty" rather than "you did not ask". So a start
    # date is always sent; this is the one used when the caller wants
    # everything available.
    EARLIEST = date(2000, 1, 1)

    # SimpleFIN hard-caps a request at 90 days and advises 45, so requests
    # are sized just inside what it recommends rather than what it allows.
    # MAX_WINDOWS bounds a sync against the daily quota of 24 requests while
    # still reaching several years back.
    WINDOW_DAYS = 44
    MAX_WINDOWS = 20

    def fetch(self, *, since: date | None = None, pending: bool = False) -> SyncResult:
        """Retrieve accounts and transactions, walking back through history.

        SimpleFIN caps a single request at 90 days — it silently truncates a
        wider range and says so in an advisory message. That cap is per
        request, not a limit on what is available, so this walks backwards in
        windows until one comes back empty. Fetching only the most recent
        window would silently miss everything older, which on a real account
        was 212 transactions.
        """
        result = SyncResult()
        seen_accounts: dict[str, Account] = {}
        signatures: set[tuple[str, str, int, str]] = set()

        window_end = date.today() + timedelta(days=1)
        floor = since or self.EARLIEST

        for _ in range(self.MAX_WINDOWS):
            window_start = max(floor, window_end - timedelta(days=self.WINDOW_DAYS))
            payload = _get(self.access_url, self._params(window_start, window_end, pending))
            self.requests_made += 1
            found = self._absorb(payload, result, seen_accounts, signatures)

            if window_start <= floor:
                break
            if not found:
                # Two consecutive empty windows would be tidier, but banks
                # rarely expose more than a year through an aggregator, and a
                # quiet month is far more common than a gap followed by more
                # history. Stopping at the first empty window keeps a sync to
                # a handful of requests against a daily quota of 24.
                break
            window_end = window_start

        result.accounts = list(seen_accounts.values())
        # Assigned once across every window: doing it per window would give
        # the same charge a different ordinal depending on which request it
        # arrived in, and it would stop matching what is already stored.
        result.transactions.sort(key=lambda t: (t.date, t.id))
        assign_occurrences(result.transactions)
        return result

    def _params(self, start: date, end: date, pending: bool) -> dict[str, str]:
        params = {
            "start-date": str(int(datetime.combine(start, datetime.min.time()).timestamp())),
            "end-date": str(int(datetime.combine(end, datetime.min.time()).timestamp())),
        }
        if pending:
            params["pending"] = "1"
        return params

    def _absorb(
        self,
        payload: dict[str, Any],
        result: SyncResult,
        seen_accounts: dict[str, Account],
        signatures: set[tuple[str, str, int, str]],
    ) -> int:
        """Merge one window's payload into the running result.

        Windows share their boundary day, so the same transaction can arrive
        twice; a signature set keeps it from being counted or stored twice.
        """
        for message in _messages_in(json.dumps(payload)):
            if message not in result.warnings:
                result.warnings.append(message)

        found = 0
        for node in payload.get("accounts") or []:
            if not isinstance(node, dict):
                continue
            external = str(node.get("id") or "")
            local_id = self.account_ids.get(external) or uuid.uuid4().hex[:12]
            self.account_ids[external] = local_id

            account = _to_account(node, local_id)
            seen_accounts.setdefault(external, account)

            raw_balance = node.get("balance")
            if raw_balance is not None and local_id not in result.balances:
                # A balance we cannot parse is not worth failing a sync
                # over; the transactions are still good.
                with contextlib.suppress(ValueError, TypeError):
                    result.balances[local_id] = Money.parse(str(raw_balance), account.currency)

            fresh = []
            for raw in node.get("transactions") or []:
                if not isinstance(raw, dict):
                    continue
                tx = _to_transaction(raw, local_id, account.currency)
                if tx is None:
                    continue
                key = (local_id, tx.date.isoformat(), tx.amount.minor, tx.description.upper())
                if key in signatures:
                    continue
                signatures.add(key)
                fresh.append(tx)
            result.transactions.extend(fresh)
            found += len(fresh)
        return found
