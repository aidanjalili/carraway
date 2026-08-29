"""Read a Venmo account through Venmo's own mobile API.

⚠️ **This is an unofficial, undocumented API.** Venmo retired its public
Developer API, so the endpoints below are the ones the Venmo iOS app talks to.
Using them has real consequences the user must opt into knowingly:

* **It is against Venmo's terms of service.** Venmo may suspend or close an
  account for automated access. Nobody can promise they will not.
* **The access token is not read-only.** Venmo issues one token for everything,
  it never expires, and anyone holding it can move money out of the account.
  Carraway therefore keeps it in the system keyring where possible, only ever
  issues GET requests against it, and offers `carraway venmo logout` to revoke
  it server-side rather than merely forgetting it locally.
* **It will break.** An undocumented endpoint changes without notice.

The password is used exactly once, in the login exchange, and is never written
anywhere. Only the resulting token is stored.

Venmo's own CSV export (`carraway import statement.csv`) needs none of this and
remains the recommended path.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..analysis.recurring import normalise_merchant
from ..core.models import Account, AccountType, Transaction, assign_occurrences
from ..core.money import Money
from .base import SyncResult

_BASE = "https://api.venmo.com/v1"
# Venmo pages transactions at 50 and ignores larger values.
_PAGE_SIZE = 50
# A stable identifier keeps Venmo from treating every sync as a new device and
# demanding two-factor again. It is random per installation, not a real device.
_DEVICE_ID_KEY = "venmo-device-id"


class VenmoError(RuntimeError):
    """Venmo rejected a request, or returned something unusable."""


class TwoFactorRequired(VenmoError):
    """Venmo wants a one-time code before it will issue a token."""

    def __init__(self, otp_secret: str) -> None:
        super().__init__("Venmo requires a two-factor code")
        self.otp_secret = otp_secret


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    device_id: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """One HTTP call. Returns (status, parsed body, response headers).

    Amounts arrive as JSON numbers, so floats are kept out by parsing them
    straight to Decimal — by the time a float exists the precision is already
    gone, and core.money refuses them for exactly that reason.
    """
    url = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    # Identifying as the iOS app is what makes these endpoints answer at all.
    request.add_header("User-Agent", "Venmo/8.30.0 (iPhone; iOS 17.0; Scale/3.00)")
    if device_id:
        request.add_header("device-id", device_id)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw, parse_float=Decimal) if raw else {}
            return response.status, parsed, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw, parse_float=Decimal) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": {"message": raw[:200]}}
        return exc.code, parsed, dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise VenmoError(f"Could not reach Venmo: {exc.reason}") from exc


def new_device_id() -> str:
    return f"carraway-{uuid.uuid4().hex[:24]}"


def log_in(username: str, password: str, device_id: str) -> tuple[str, str]:
    """Exchange credentials for a token. Returns (token, user id).

    Raises TwoFactorRequired when Venmo wants a code, carrying the OTP secret
    the follow-up call needs. The password is not retained by this module.
    """
    status, body, headers = _request(
        "POST",
        "/oauth/access_token",
        device_id=device_id,
        body={
            "phone_email_or_username": username,
            "client_id": "1",
            "password": password,
        },
    )
    if status == 401:
        secret = headers.get("venmo-otp-secret") or headers.get("Venmo-Otp-Secret")
        if secret:
            raise TwoFactorRequired(secret)
        raise VenmoError("Venmo rejected those credentials")
    if status != 200:
        raise VenmoError(_message(body, f"login failed with HTTP {status}"))
    return _token_from(body)


def send_two_factor_code(otp_secret: str, device_id: str) -> None:
    """Ask Venmo to text the user a one-time code."""
    status, body, _ = _request(
        "POST",
        "/account/two-factor/token",
        device_id=device_id,
        body={"via": "sms"},
        headers={"venmo-otp-secret": otp_secret},
    )
    if status not in (200, 201):
        raise VenmoError(_message(body, f"could not send a code (HTTP {status})"))


def submit_two_factor_code(code: str, otp_secret: str, device_id: str) -> tuple[str, str]:
    """Complete login with the code Venmo sent. Returns (token, user id)."""
    status, body, _ = _request(
        "POST",
        "/oauth/access_token?client_id=1",
        device_id=device_id,
        headers={"venmo-otp-secret": otp_secret, "Venmo-Otp": code.strip()},
    )
    if status != 200:
        raise VenmoError(_message(body, f"that code was not accepted (HTTP {status})"))
    return _token_from(body)


def log_out(token: str) -> bool:
    """Revoke the token at Venmo, not merely locally.

    Worth doing properly: the token never expires on its own, so a copy that
    leaks stays valid until someone revokes it.
    """
    status, _, _ = _request("DELETE", "/oauth/access_token", token=token)
    return status in (200, 204)


def _token_from(body: dict[str, Any]) -> tuple[str, str]:
    token = body.get("access_token")
    if not token:
        raise VenmoError("Venmo did not return an access token")
    user = body.get("user") or body.get("balance", {}).get("user") or {}
    user_id = str(user.get("id") or "")
    if not user_id:
        raise VenmoError("Venmo did not identify the account")
    return str(token), user_id


def _message(body: dict[str, Any], fallback: str) -> str:
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return fallback


def _person(node: Any) -> str:
    """A display name for whoever is at the other end of a payment."""
    if not isinstance(node, dict):
        return ""
    user = node.get("user") if isinstance(node.get("user"), dict) else node
    for key in ("display_name", "name", "username"):
        value = user.get(key)
        if value:
            return str(value)
    return ""


def _to_transaction(story: dict[str, Any], me: str, account_id: str) -> Transaction | None:
    """Convert one Venmo story into a Transaction, or None if it is not money.

    Sign convention is the whole risk here. Venmo reports an amount without a
    sign and describes the direction separately, so it has to be derived:

    * `action: "pay"` — the actor sent money to the target.
    * `action: "charge"` — the actor requested money, so it flows the other way.

    Money leaving the user is negative, matching every other importer.
    """
    payment = story.get("payment")
    if not isinstance(payment, dict):
        return None
    # Only settled money has actually moved; a pending or cancelled request has
    # not, and counting it would misstate every total downstream.
    if str(payment.get("status", "")).lower() != "settled":
        return None

    raw_amount = payment.get("amount")
    if raw_amount is None:
        return None
    # Decimal from the JSON text, never a float: see _request.
    magnitude = abs(Decimal(str(raw_amount)))

    actor = payment.get("actor") or {}
    target = payment.get("target") or {}
    actor_id = str((actor.get("user") or actor).get("id") or "")
    target_id = str((target.get("user") or target).get("id") or "")
    action = str(payment.get("action", "pay")).lower()

    if action == "charge":
        # A settled charge means the target paid the actor.
        payer_id, payer, payee = target_id, _person(target), _person(actor)
    else:
        payer_id, payer, payee = actor_id, _person(actor), _person(target)

    outgoing = payer_id == me
    amount = Money.parse(f"-{magnitude}" if outgoing else str(magnitude))
    # The counterparty is always the other person. Naming the user in their own
    # ledger tells them nothing, and would collapse every incoming payment into
    # a single merchant under their own name.
    counterparty = payee if outgoing else payer

    when = payment.get("date_completed") or story.get("date_created")
    try:
        moment = datetime.fromisoformat(str(when).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None

    note = str(story.get("note") or payment.get("note") or "").strip()
    counterparty = counterparty or "Venmo"
    description = f"{counterparty} - {note}" if note else counterparty

    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account_id,
        date=moment,
        amount=amount,
        description=description,
        # Normalised from the counterparty alone: the note is different every
        # time and would fragment one person into many merchants, hiding a
        # recurring payment to a housemate or a regular split.
        merchant=normalise_merchant(counterparty),
    )


def fetch_transactions(
    token: str, user_id: str, account_id: str, *, since: date | None = None, max_pages: int = 40
) -> tuple[list[Transaction], list[str]]:
    """Page through the user's own Venmo history, newest first.

    `max_pages` is a stop rather than a target: without it a long history plus
    an unexpected pagination response could loop indefinitely against an API
    that is undocumented and free to change its mind.
    """
    transactions: list[Transaction] = []
    warnings: list[str] = []
    before_id: str | None = None

    for _ in range(max_pages):
        path = f"/stories/target-or-actor/{user_id}?limit={_PAGE_SIZE}"
        if before_id:
            path += f"&before_id={before_id}"
        status, body, _ = _request("GET", path, token=token)
        if status == 401:
            raise VenmoError("Venmo rejected the stored token; run 'carraway venmo login' again")
        if status != 200:
            warnings.append(_message(body, f"stopped early: HTTP {status}"))
            break

        stories = body.get("data") or []
        if not stories:
            break

        reached_start = False
        for story in stories:
            converted = _to_transaction(story, user_id, account_id)
            if converted is None:
                continue
            if since and converted.date < since:
                reached_start = True
                continue
            transactions.append(converted)

        if reached_start:
            break
        before_id = str(stories[-1].get("id") or "")
        if not before_id:
            break
    else:
        warnings.append(f"stopped after {max_pages} pages; re-run to fetch older history")

    assign_occurrences(transactions)
    return transactions, warnings


class VenmoProvider:
    """A `sync.Provider` backed by Venmo's mobile API."""

    name = "venmo"

    def __init__(self, token: str, user_id: str, account_id: str) -> None:
        self.token = token
        self.user_id = user_id
        self.account_id = account_id

    def account(self) -> Account:
        return Account(
            id=self.account_id,
            name="Venmo",
            type=AccountType.CASH,
            institution="Venmo",
            external_id=self.user_id,
        )

    def fetch(self, *, since: date | None = None) -> SyncResult:
        transactions, warnings = fetch_transactions(
            self.token, self.user_id, self.account_id, since=since
        )
        return SyncResult(accounts=[self.account()], transactions=transactions, warnings=warnings)
