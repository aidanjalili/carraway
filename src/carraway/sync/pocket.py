"""Collect what you typed on your phone, and tell it what is left in the budget.

The other half of this lives on a small server; see the carraway-pocket
project. The division is deliberate and worth restating here, because it is
the reason this module is so short:

    the laptop is the source of truth, the server is a post box.

So this module only ever does three things — take the entries waiting there,
confirm it has them, and publish a summary small enough that losing it would
not matter. It never sends transactions, balances, account numbers, or
anything that could be used to reach a bank.

The order of the first two matters. Entries are written to the local database
*before* they are claimed, so a connection that dies between the two leaves an
entry on the server to be collected again rather than gone. Carraway already
refuses to import the same transaction twice, so arriving twice is survivable
in a way that never arriving is not.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from ..core.models import Transaction
from ..core.money import Money
from .net import urlopen

_USER_AGENT = "Carraway/0.1 (+https://github.com/aidanjalili/carraway)"

# Long enough for a phone-sized payload over a bad connection, short enough
# that opening the app never feels like it has hung.
TIMEOUT = 20.0


class PocketError(RuntimeError):
    """Something went wrong talking to the inbox, phrased for a person."""


@dataclass(frozen=True, slots=True)
class InboxEntry:
    """One entry as the phone recorded it."""

    id: str
    occurred_on: date
    amount: Money
    description: str
    category: str
    account: str
    # "spend" is a movement. "count" is the user saying how much cash is
    # actually in their wallet, which is not a transaction: the difference
    # against what this ledger implies is, and only this end can work that
    # out, because the phone never learns the balance.
    kind: str = "spend"

    @property
    def is_count(self) -> bool:
        return self.kind == "count"

    @classmethod
    def from_json(cls, payload: dict) -> InboxEntry:
        return cls(
            id=str(payload["id"]),
            occurred_on=date.fromisoformat(str(payload["occurred_on"])),
            # A decimal string on the wire, parsed exactly.
            #
            # The float check is repeated here rather than left to the server.
            # `str()` on a float launders it into something Money.parse will
            # happily accept -- 12.345 becomes "12.345" and quietly rounds --
            # so deferring to the far end meant the one rule Carraway enforces
            # everywhere had a hole in it on the only path that crosses a
            # network. The server is the user's own box, but it is also the
            # part that faces the internet, which makes it the part to treat
            # as a claim rather than a fact.
            amount=_amount(payload["amount"]),
            description=str(payload["description"]),
            category=str(payload.get("category") or ""),
            account=str(payload.get("account") or "Cash"),
            kind=str(payload.get("kind") or "spend"),
        )


def _amount(raw: object) -> Money:
    """Parse an amount off the wire, refusing anything that lost precision."""
    if isinstance(raw, float):
        raise ValueError(f"amount must be a decimal string, not a float; got {raw!r}")
    return Money.parse(str(raw))


class PocketClient:
    """Talks to one inbox. Holds no state beyond where it is and who it is."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": _USER_AGENT,
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PocketError(
                    "This computer is not paired with your Pocket inbox any more. "
                    "Pair it again from Settings."
                ) from exc
            detail = ""
            # The error body is a courtesy, not a contract. If it is missing
            # or unparseable the status code still tells the user something.
            with contextlib.suppress(Exception):
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            raise PocketError(f"The inbox refused that ({exc.code}). {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise PocketError(f"Could not reach {self.base_url}: {exc.reason}") from exc
        return json.loads(raw) if raw else {}

    def pending(self) -> list[InboxEntry]:
        """Everything waiting to be collected. Changes nothing on the server."""
        payload = self._request("GET", "/api/entries")
        return [InboxEntry.from_json(item) for item in payload.get("entries", [])]

    def claim(self, ids: list[str]) -> int:
        """Tell the server these are safely stored, so it can forget them."""
        if not ids:
            return 0
        return int(self._request("POST", "/api/entries/claim", {"ids": ids}).get("claimed", 0))

    def publish(self, snapshot: dict) -> str:
        """Put a summary where the phone can read it."""
        return str(self._request("PUT", "/api/snapshot", snapshot).get("as_of", ""))

    def create_pairing(self, label: str = "iPhone") -> dict:
        """Mint a link for another device. Returns `{code, url, expires_at}`."""
        return self._request("POST", "/api/pairings", {"label": label})

    def devices(self) -> list[dict]:
        return list(self._request("GET", "/api/devices").get("devices", []))

    def revoke(self, device_id: str) -> bool:
        return bool(self._request("DELETE", f"/api/devices/{device_id}").get("revoked"))

    def status(self) -> dict:
        return self._request("GET", "/api/status")


def redeem(pairing_url: str, name: str = "Carraway on this computer") -> tuple[str, str]:
    """Trade a one-time pairing link for `(base_url, token)`.

    The link comes from `pocket-admin pair` on the server the first time, and
    from an already-paired device after that. Redeeming it is what makes this
    computer trusted; the token then lives in the system keyring, not here.
    """
    url = pairing_url.strip()
    if "/pair/" not in url:
        raise PocketError(
            "That does not look like a pairing link. It should end in /pair/ followed by a code."
        )
    base = url.split("/pair/", 1)[0]
    if not base.startswith("https://"):
        # The token comes back in this response. Over plain HTTP it would be
        # readable by anything on the path, which is the whole credential.
        raise PocketError("A pairing link must be https, so the token stays private.")

    request = urllib.request.Request(
        url,
        data=json.dumps({"name": name, "want_token": True}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        if exc.code == 400:
            raise PocketError(
                detail or "That pairing link has already been used, or has expired."
            ) from exc
        raise PocketError(f"Pairing failed ({exc.code}). {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise PocketError(f"Could not reach {base}: {exc.reason}") from exc

    token = str(payload.get("token") or "")
    if not token:
        raise PocketError("The server paired this computer but sent no token back.")
    return base, token


def to_transactions(
    entries: list[InboxEntry], accounts_by_name: dict[str, str]
) -> tuple[list[Transaction], list[InboxEntry]]:
    """Turn entries into transactions. Returns `(ready, unmatched)`.

    An entry naming an account this ledger does not have is *not* guessed at
    and *not* dropped — it comes back as unmatched so the caller can say so.
    Filing someone's cash under the wrong account silently is worse than
    telling them the name did not match.
    """
    import uuid

    ready: list[Transaction] = []
    unmatched: list[InboxEntry] = []
    lowered = {name.lower(): account_id for name, account_id in accounts_by_name.items()}

    for entry in entries:
        # A count is not a movement and has no transaction of its own. The
        # caller reconciles it against the ledger and writes the difference.
        if entry.is_count:
            continue
        account_id = lowered.get(entry.account.lower())
        if account_id is None:
            unmatched.append(entry)
            continue
        ready.append(
            Transaction(
                id=uuid.uuid4().hex,
                account_id=account_id,
                date=entry.occurred_on,
                amount=entry.amount,
                description=entry.description,
                merchant=entry.description,
                category=entry.category,
            )
        )
    return ready, unmatched
