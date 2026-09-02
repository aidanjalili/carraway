"""Collecting what was typed on the phone.

The rules that matter here are about not losing things: an entry naming an
unknown account must not be guessed at or dropped, and nothing is claimed
from the server until it is safely in the local database.
"""

import json
import urllib.error
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
