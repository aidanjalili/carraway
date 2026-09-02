"""Collecting what was typed on the phone.

The rules that matter here are about not losing things: an entry naming an
unknown account must not be guessed at or dropped, and nothing is claimed
from the server until it is safely in the local database.
"""

import json
import urllib.error
import uuid
from datetime import date

import pytest

from carraway.core.money import Money
from carraway.sync.pocket import InboxEntry, PocketClient, PocketError, to_transactions


def _entry(**over) -> InboxEntry:
    fields = {
        "id": "e1",
        "occurred_on": date(2026, 8, 31),
        "amount": Money.parse("-24.00"),
        "description": "Farmers market",
        "category": "Groceries",
        "account": "Cash",
    }
    fields.update(over)
    return InboxEntry(**fields)


def test_an_entry_parses_from_the_wire_exactly():
    entry = InboxEntry.from_json(
        {
            "id": "abc",
            "occurred_on": "2026-08-31",
            "amount": "-24.99",
            "description": "Market",
            "category": "Groceries",
            "account": "Cash",
        }
    )
    # A decimal string, parsed exactly. 24.99 as a float is not 24.99.
    assert entry.amount == Money.parse("-24.99")
    assert entry.occurred_on == date(2026, 8, 31)


def test_entries_become_transactions_on_the_named_account():
    ready, unmatched = to_transactions([_entry()], {"Cash": "cash-id"})
    assert unmatched == []
    assert ready[0].account_id == "cash-id"
    assert ready[0].amount == Money.parse("-24.00")
    assert ready[0].description == "Farmers market"


def test_the_account_name_is_matched_case_insensitively():
    ready, unmatched = to_transactions([_entry(account="cash")], {"Cash": "cash-id"})
    assert unmatched == []
    assert ready[0].account_id == "cash-id"


def test_an_unknown_account_is_reported_rather_than_guessed():
    # Filing someone's cash under the wrong account silently is worse than
    # telling them the name did not match.
    ready, unmatched = to_transactions([_entry(account="Wallet")], {"Cash": "cash-id"})
    assert ready == []
    assert [e.description for e in unmatched] == ["Farmers market"]


def test_income_keeps_its_sign():
    ready, _ = to_transactions([_entry(amount=Money.parse("45.00"))], {"Cash": "c"})
    assert ready[0].amount.minor > 0


class _Response:
    """The bit of an HTTP response `_request` actually uses."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _client(monkeypatch, answer):
    """A client whose network layer is replaced, but whose logic is real.

    Patched at `urlopen` rather than at `_request`: overriding `_request`
    would skip the error handling this file is mostly here to check, and the
    tests would only be proving the stub works.
    """
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.method, request.full_url, request.data))
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    monkeypatch.setattr("carraway.sync.pocket.urlopen", fake_urlopen)
    client = PocketClient("https://example.invalid", "token")
    return client, calls


def test_pending_reads_the_entries_list(monkeypatch):
    body = json.dumps(
        {
            "entries": [
                {
                    "id": "e1",
                    "occurred_on": "2026-08-31",
                    "amount": "-1.00",
                    "description": "x",
                    "category": "",
                    "account": "Cash",
                }
            ]
        }
    )
    client, calls = _client(monkeypatch, body)
    assert [e.id for e in client.pending()] == ["e1"]
    assert calls[0][0] == "GET"


def test_the_bearer_token_is_sent(monkeypatch):
    client, _ = _client(monkeypatch, '{"entries": []}')
    seen = {}

    def capture(request, timeout=None):
        seen.update(request.headers)
        return _Response('{"entries": []}')

    monkeypatch.setattr("carraway.sync.pocket.urlopen", capture)
    client.pending()
    assert seen["Authorization"] == "Bearer token"


def test_claiming_nothing_makes_no_request(monkeypatch):
    # Not just an optimisation: an empty claim is a no-op, and sending one
    # would be a request that can fail for no reason.
    client, calls = _client(monkeypatch, "{}")
    assert client.claim([]) == 0
    assert calls == []


def test_a_401_says_to_pair_again_rather_than_showing_a_status_code(monkeypatch):
    error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    client, _ = _client(monkeypatch, error)
    with pytest.raises(PocketError, match="not paired"):
        client.pending()


def test_an_unreachable_server_names_the_address(monkeypatch):
    error = urllib.error.URLError("Name or service not known")
    client, _ = _client(monkeypatch, error)
    with pytest.raises(PocketError, match="example.invalid"):
        client.pending()


def test_publish_sends_the_snapshot_body(monkeypatch):
    client, calls = _client(monkeypatch, '{"as_of": "2026-08-31T00:00:00+00:00"}')
    client.publish({"budgets": []})
    method, url, data = calls[0]
    assert method == "PUT"
    assert url.endswith("/api/snapshot")
    assert json.loads(data) == {"budgets": []}


# -- counting your wallet from the phone --------------------------------


def _wire_entry(**fields):
    from carraway.sync.pocket import InboxEntry

    base = {
        "id": uuid.uuid4().hex,
        "occurred_on": date(2026, 9, 2),
        "amount": Money.parse("-12.00"),
        "description": "Coffee",
        "category": "Dining",
        "account": "Cash",
    }
    return InboxEntry(**{**base, **fields})


def test_a_count_carries_its_kind_off_the_wire():
    from carraway.sync.pocket import InboxEntry

    entry = InboxEntry.from_json(
        {
            "id": "e1",
            "occurred_on": "2026-09-02",
            "amount": "40.00",
            "description": "Wallet count",
            "kind": "count",
        }
    )
    assert entry.is_count is True


def test_an_entry_with_no_kind_is_a_spend():
    from carraway.sync.pocket import InboxEntry

    entry = InboxEntry.from_json(
        {"id": "e1", "occurred_on": "2026-09-02", "amount": "-8.00", "description": "Tea"}
    )
    assert entry.is_count is False


def test_a_count_never_becomes_a_transaction_of_its_own():
    """It is a statement about a total, not a movement. Turning it into one
    would add the whole wallet to the ledger as though it had been spent."""
    from carraway.sync.pocket import to_transactions

    ready, unmatched = to_transactions(
        [_wire_entry(kind="count", amount=Money.parse("40.00"), description="Wallet count")],
        {"Cash": "cash"},
    )
    assert ready == []
    assert unmatched == []


def test_spends_alongside_a_count_still_come_through():
    from carraway.sync.pocket import to_transactions

    ready, _ = to_transactions(
        [
            _wire_entry(),
            _wire_entry(kind="count", amount=Money.parse("40.00")),
        ],
        {"Cash": "cash"},
    )
    assert len(ready) == 1
    assert ready[0].description == "Coffee"


# -- what a hostile inbox could do to the ledger ------------------------
#
# The server is the user's own box, but it is the one part of Carraway that
# faces the internet, so it is the part to assume is lying. Everything it
# sends must be treated as a claim rather than a fact.


def _hostile(payload: dict):
    from carraway.sync.pocket import InboxEntry

    return InboxEntry.from_json(payload)


def test_an_entry_naming_an_unknown_account_cannot_pick_one(tmp_path):
    """The one thing a hostile inbox must not do is get money filed
    somewhere the user did not choose."""
    from carraway.sync.pocket import to_transactions

    entry = _wire_entry(account="Definitely Not An Account")
    ready, unmatched = to_transactions([entry], {"Cash": "cash", "Card": "card"})
    assert ready == []
    assert unmatched == [entry]


def test_an_entry_cannot_choose_its_own_transaction_id():
    """Ids are minted here. A server that could pick them could collide with
    an existing transaction and overwrite it."""
    from carraway.sync.pocket import to_transactions

    ready, _ = to_transactions([_wire_entry(id="../../etc/passwd")], {"Cash": "cash"})
    assert ready[0].id != "../../etc/passwd"
    assert len(ready[0].id) == 32


def test_a_float_amount_from_the_server_is_refused():
    with pytest.raises((TypeError, ValueError, ArithmeticError)):
        _hostile({"id": "e", "occurred_on": "2026-09-02", "amount": 12.5, "description": "x"})


@pytest.mark.parametrize("amount", ["", "abc", "NaN", "Infinity", "1e400"])
def test_junk_amounts_from_the_server_are_refused(amount):
    with pytest.raises((TypeError, ValueError, ArithmeticError)):
        _hostile({"id": "e", "occurred_on": "2026-09-02", "amount": amount, "description": "x"})


@pytest.mark.parametrize("when", ["", "not-a-date", "2026-13-01", "0000-00-00"])
def test_junk_dates_from_the_server_are_refused(when):
    with pytest.raises(ValueError):
        _hostile({"id": "e", "occurred_on": when, "amount": "-1.00", "description": "x"})


def test_a_missing_field_is_an_error_rather_than_a_guess():
    with pytest.raises((KeyError, ValueError, TypeError)):
        _hostile({"id": "e", "description": "x"})


def test_an_unknown_kind_is_treated_as_a_spend_not_as_a_count():
    """A count writes a correction, so anything unrecognised must fall to the
    safer of the two rather than the one that adjusts a balance."""
    entry = _hostile(
        {
            "id": "e",
            "occurred_on": "2026-09-02",
            "amount": "-1.00",
            "description": "x",
            "kind": "something-new",
        }
    )
    assert entry.is_count is False
