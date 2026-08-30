"""Provider adapters, and the credential store they depend on."""

import json
from datetime import date
from unittest.mock import patch

import pytest

from carraway.core.money import Money
from carraway.sync import credentials
from carraway.sync.simplefin import SimpleFinError, SimpleFinProvider, claim_setup_token

ACCOUNTS_PAYLOAD = {
    "errlist": [{"code": "con.auth", "msg": "Chase needs reauthorisation"}],
    "accounts": [
        {
            "id": "acc-1",
            "name": "Everyday Checking",
            "currency": "USD",
            "balance": "1520.44",
            "org": {"name": "Wells Fargo"},
            "transactions": [
                {
                    "id": "t1",
                    "posted": 1767139200,
                    "amount": "-15.49",
                    "description": "NETFLIX.COM",
                },
                {
                    "id": "t2",
                    "posted": 1767225600,
                    "amount": "2612.44",
                    "description": "ACME PAYROLL",
                },
            ],
        },
        {
            "id": "acc-2",
            "name": "Freedom Credit Card",
            "currency": "USD",
            "balance": "-842.10",
            "org": {"name": "Chase"},
            "transactions": [],
        },
    ],
}


def test_simplefin_maps_accounts_and_transactions():
    with patch("carraway.sync.simplefin._get", return_value=ACCOUNTS_PAYLOAD):
        result = SimpleFinProvider("https://u:p@example.org/simplefin").fetch()

    assert [a.name for a in result.accounts] == ["Everyday Checking", "Freedom Credit Card"]
    # Type is inferred from the name, since SimpleFIN does not report one.
    assert str(result.accounts[1].type) == "credit_card"
    assert result.accounts[0].institution == "Wells Fargo"

    # SimpleFIN's sign convention already matches ours: negative is money out.
    amounts = {t.description: t.amount for t in result.transactions}
    assert amounts["NETFLIX.COM"] == Money.parse("-15.49")
    assert amounts["ACME PAYROLL"] == Money.parse("2612.44")


def test_a_failing_connection_does_not_lose_the_others():
    # One bank needing reauthorisation must not cost the user the accounts
    # that synced fine, so provider errors are warnings rather than exceptions.
    with patch("carraway.sync.simplefin._get", return_value=ACCOUNTS_PAYLOAD):
        result = SimpleFinProvider("https://u:p@example.org/simplefin").fetch()

    assert result.warnings == ["Chase needs reauthorisation"]
    assert len(result.accounts) == 2


def test_resyncing_reuses_local_account_ids():
    # Without this a second sync creates a parallel set of duplicate accounts.
    provider = SimpleFinProvider("https://u:p@example.org/simplefin")
    with patch("carraway.sync.simplefin._get", return_value=ACCOUNTS_PAYLOAD):
        first = provider.fetch()
        second = provider.fetch()

    assert [a.id for a in first.accounts] == [a.id for a in second.accounts]


def test_amounts_never_pass_through_a_float():
    # json.loads would hand back 0.1 + 0.2 style floats; parse_float=Decimal is
    # what keeps a cent from going missing between the bank and the ledger.
    payload = json.loads('{"amount": 1234.56}', parse_float=__import__("decimal").Decimal)
    assert str(payload["amount"]) == "1234.56"
    assert Money.parse(str(payload["amount"])).minor == 123456


def test_setup_token_must_be_base64_and_https():
    with pytest.raises(SimpleFinError, match="setup token"):
        claim_setup_token("this is not base64 at all !!")

    import base64

    plain = base64.b64encode(b"http://insecure.example.org/claim").decode()
    with pytest.raises(SimpleFinError, match="HTTPS"):
        claim_setup_token(plain)


def test_credentials_round_trip(tmp_path, monkeypatch):
    # Force the file fallback: the keyring is the machine's, not the test's.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "_keyring", lambda: None)

    assert credentials.load("unit-test-key") is None
    credentials.store("unit-test-key", "s3cret")
    assert credentials.load("unit-test-key") == "s3cret"

    stored = tmp_path / "carraway" / "unit-test-key.secret"
    # A secret written world-readable would be worse than not storing it.
    assert stored.stat().st_mode & 0o077 == 0
    assert credentials.delete("unit-test-key") is True
    assert credentials.load("unit-test-key") is None


def test_credentials_survive_a_broken_keyring(tmp_path, monkeypatch):
    # A keyring that imports but has no working backend raises only on use.
    # Falling through to the file beats losing what the user just pasted.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    class Broken:
        def set_password(self, *_):
            raise RuntimeError("no Secret Service")

        def get_password(self, *_):
            raise RuntimeError("no Secret Service")

    monkeypatch.setattr(credentials, "_keyring", lambda: Broken())
    credentials.store("fallback-key", "value")
    assert credentials.load("fallback-key") == "value"
    credentials.delete("fallback-key")


def test_since_becomes_a_start_date_parameter():
    seen = {}

    def fake_get(url, params):
        seen.update(params)
        return {"accounts": []}

    with patch("carraway.sync.simplefin._get", side_effect=fake_get):
        SimpleFinProvider("https://u:p@example.org/simplefin").fetch(since=date(2026, 1, 1))

    assert "start-date" in seen
    assert seen["start-date"].isdigit()


def test_a_token_can_be_checked_without_being_spent():
    # Claiming is one-use, so a mangled paste must be catchable before it costs
    # the user a trip back to SimpleFIN for a fresh token.
    import base64

    from carraway.sync.simplefin import decode_setup_token

    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/ABC").decode()
    assert decode_setup_token(token) == "https://bridge.example.org/simplefin/claim/ABC"


def test_a_wrapped_or_unpadded_paste_still_decodes():
    # Tokens get wrapped by terminals and stripped of padding by web UIs;
    # neither should look like a bad token to the user.
    import base64

    from carraway.sync.simplefin import decode_setup_token

    url = b"https://bridge.example.org/simplefin/claim/ABCDE"
    token = base64.b64encode(url).decode()
    wrapped = token[:20] + "\n  " + token[20:]
    assert decode_setup_token(wrapped) == url.decode()
    assert decode_setup_token(token.rstrip("=")) == url.decode()


def test_useless_pastes_say_what_is_wrong():
    from carraway.sync.simplefin import decode_setup_token

    with pytest.raises(SimpleFinError, match="not a valid SimpleFIN setup token"):
        decode_setup_token("this is clearly not base64 !!")
    with pytest.raises(SimpleFinError, match="No setup token given"):
        decode_setup_token("   ")


def test_every_request_identifies_the_app():
    # Cloudflare fronts SimpleFIN Bridge and bans urllib's default User-Agent
    # by signature, returning "error code: 1010" before SimpleFIN ever sees the
    # request. The symptom is a 403 indistinguishable from an already-claimed
    # token, so this header is load-bearing rather than cosmetic.
    import base64
    from unittest.mock import MagicMock

    from carraway.sync import simplefin

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        response = MagicMock()
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        response.read.return_value = b"https://user:pass@bridge.example.org/simplefin"
        return response

    token = base64.b64encode(b"https://bridge.example.org/claim/ABC").decode()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        simplefin.claim_setup_token(token)

    assert "carraway" in captured["headers"]["user-agent"].lower()


def test_an_edge_block_is_not_reported_as_a_claimed_token():
    # These are opposite problems: one means the token is spent, the other
    # means it was never seen and is still good. Conflating them sends the user
    # to generate tokens that were never the issue.
    from carraway.sync.simplefin import _is_edge_block

    assert _is_edge_block("error code: 1010")
    assert _is_edge_block("Attention Required! | Cloudflare")
    assert not _is_edge_block("Forbidden (was it already claimed?)")
    assert not _is_edge_block("")


def test_embedded_credentials_become_an_auth_header():
    # SimpleFIN returns https://user:pass@host/simplefin, which urllib cannot
    # use — it parses the password as a port and raises ValueError. Splitting
    # them also keeps the secret out of any URL that might be logged or land
    # in a traceback.
    import base64

    from carraway.sync.simplefin import split_credentials

    url, auth = split_credentials("https://USER123:PASSabc@bridge.example.org/simplefin")
    assert url == "https://bridge.example.org/simplefin"
    assert base64.b64decode(auth.split()[1]).decode() == "USER123:PASSabc"

    # A URL without credentials is left exactly as it was.
    assert split_credentials("https://bridge.example.org/simplefin") == (
        "https://bridge.example.org/simplefin",
        "",
    )


def test_both_error_field_names_are_read():
    # The published protocol says `errlist` with {code, msg} objects; the live
    # Bridge returns `errors` with plain strings. A response that explains
    # itself should not be thrown away over a field name.
    from carraway.sync.simplefin import _messages_in

    assert _messages_in('{"errors":["No connections available."],"accounts":[]}') == [
        "No connections available."
    ]
    assert _messages_in('{"errlist":[{"code":"con.auth","msg":"Reauthorise Chase"}]}') == [
        "Reauthorise Chase"
    ]
    assert _messages_in("not json at all") == []
    assert _messages_in("") == []


def test_plain_string_errors_become_warnings():
    payload = {"errors": ["Chase needs reauthorisation"], "accounts": []}
    with patch("carraway.sync.simplefin._get", return_value=payload):
        result = SimpleFinProvider("https://u:p@example.org/simplefin").fetch()
    assert result.warnings == ["Chase needs reauthorisation"]


def _fresh_db(tmp_path):
    from carraway.core import db as core_db

    return core_db.connect(tmp_path / "sync.db")


def test_a_manual_refresh_is_refused_until_the_cooldown_passes(tmp_path):
    from datetime import datetime, timedelta

    from carraway.core import db as core_db
    from carraway.sync import budget as sync_worker

    conn = _fresh_db(tmp_path)
    # Nothing has run, so nothing is in the way.
    assert sync_worker.refusal_reason(conn) is None

    core_db.set_setting(conn, "last_sync_at", datetime.now().isoformat())
    reason = sync_worker.refusal_reason(conn)
    assert reason and "Try again" in reason

    # Once the cooldown has passed it is allowed again.
    earlier = datetime.now() - sync_worker.MANUAL_COOLDOWN - timedelta(seconds=1)
    core_db.set_setting(conn, "last_sync_at", earlier.isoformat())
    assert sync_worker.refusal_reason(conn) is None


def test_the_daily_budget_stops_patient_clicking(tmp_path):
    from carraway.sync import budget as sync_worker

    conn = _fresh_db(tmp_path)
    assert sync_worker.requests_left(conn) == sync_worker.DAILY_REQUEST_BUDGET

    # A cooldown alone would not stop someone clicking every few minutes all
    # day, so the budget is what actually protects the quota.
    sync_worker.record_usage(conn, sync_worker.DAILY_REQUEST_BUDGET)
    assert sync_worker.requests_left(conn) == 0
    reason = sync_worker.refusal_reason(conn)
    assert reason and "used up" in reason


def test_a_drained_budget_also_stops_the_automatic_sync(tmp_path):
    from carraway.sync import budget as sync_worker

    conn = _fresh_db(tmp_path)
    assert sync_worker.is_due(conn)
    sync_worker.record_usage(conn, sync_worker.DAILY_REQUEST_BUDGET)
    # Opening the window repeatedly must not spend what is left either.
    assert not sync_worker.is_due(conn)


def test_usage_resets_when_the_day_rolls_over(tmp_path):
    from carraway.core import db as core_db
    from carraway.sync import budget as sync_worker

    conn = _fresh_db(tmp_path)
    core_db.set_setting(conn, "sync_requests_today", {"date": "2020-01-01", "requests": 99})
    assert sync_worker.requests_left(conn) == sync_worker.DAILY_REQUEST_BUDGET


def test_the_budget_leaves_room_for_a_scheduled_sync():
    from carraway.sync import budget as sync_worker

    # A background timer failing silently on quota is worse than a button that
    # says "not yet", so the budget must sit below the provider's own limit.
    assert sync_worker.DAILY_REQUEST_BUDGET < 24
