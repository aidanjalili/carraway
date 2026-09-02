"""The encryption that stands between a stolen droplet and a year of spending.

Everything here is either "does it actually protect the data" or "does it
fail loudly rather than quietly", because a cipher that returns plausible
rubbish on a wrong key is worse than one that will not run at all.
"""

from __future__ import annotations

import base64
import json

import pytest

from carraway.sync import vault


def _payload():
    return {
        "transactions": [
            {"date": "2026-09-01", "description": "COFFEE", "amount": "-4.50"},
            {"date": "2026-09-02", "description": "RENT", "amount": "-934.37"},
        ]
    }


# -- the key ------------------------------------------------------------


def test_a_key_has_enough_entropy_to_be_worth_generating():
    key = vault.new_key()
    assert len(key) == 25
    assert set(key) <= set(vault.KEY_ALPHABET)
    # 25 characters of a 32-letter alphabet is 125 bits.
    assert len(vault.KEY_ALPHABET) == 32
    assert vault.format_key(key).count("-") == 4


def test_no_two_keys_are_the_same():
    assert len({vault.new_key() for _ in range(200)}) == 200


def test_the_key_avoids_the_characters_that_cannot_be_told_apart():
    """It is read off a laptop and typed into a phone."""
    assert not set(vault.KEY_ALPHABET) & set("ILOU")


def test_a_key_is_shown_grouped_and_read_back_however_it_is_typed():
    key = vault.new_key()
    shown = vault.format_key(key)
    assert vault.normalise_key(shown) == key
    assert vault.normalise_key(shown.lower()) == key
    assert vault.normalise_key(f"  {shown}  ") == key
    assert vault.normalise_key(shown.replace("-", " ")) == key


def test_look_alike_characters_are_forgiven():
    assert vault.normalise_key("O1L2") == "0112"


# -- sealing and opening ------------------------------------------------


def test_what_goes_in_comes_back_out():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    assert vault.open_sealed(sealed, key) == _payload()


def test_the_ciphertext_does_not_contain_the_plaintext():
    """The whole point: a stolen blob must not be readable."""
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    blob = json.dumps(sealed.as_json())
    for secret in ("COFFEE", "RENT", "934.37", "transactions"):
        assert secret not in blob
    # Nor after the base64 comes off.
    raw = base64.b64decode(sealed.ciphertext)
    assert b"COFFEE" not in raw
    assert b"RENT" not in raw


def test_the_key_is_never_in_the_blob():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    assert key not in json.dumps(sealed.as_json())


def test_a_wrong_key_raises_rather_than_returning_rubbish():
    """AES-GCM authenticates, so this cannot produce a plausible lie."""
    sealed = vault.seal(_payload(), vault.new_key(), iterations=1000)
    with pytest.raises(vault.VaultError):
        vault.open_sealed(sealed, vault.new_key())


def test_a_key_that_is_one_character_out_is_still_wrong():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    wrong = ("2" if key[0] != "2" else "3") + key[1:]
    with pytest.raises(vault.VaultError):
        vault.open_sealed(sealed, wrong)


def test_tampering_with_the_ciphertext_is_detected():
    """A server that changes a byte must produce a failure, not a lie."""
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    raw = bytearray(base64.b64decode(sealed.ciphertext))
    raw[0] ^= 0x01
    tampered = vault.Sealed(
        salt=sealed.salt,
        nonce=sealed.nonce,
        ciphertext=base64.b64encode(bytes(raw)).decode(),
        iterations=1000,
    )
    with pytest.raises(vault.VaultError):
        vault.open_sealed(tampered, key)


def test_tampering_with_the_nonce_is_detected():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    raw = bytearray(base64.b64decode(sealed.nonce))
    raw[0] ^= 0x01
    tampered = vault.Sealed(
        salt=sealed.salt,
        nonce=base64.b64encode(bytes(raw)).decode(),
        ciphertext=sealed.ciphertext,
        iterations=1000,
    )
    with pytest.raises(vault.VaultError):
        vault.open_sealed(tampered, key)


def test_tampering_with_the_salt_is_detected():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    tampered = vault.Sealed(
        salt=base64.b64encode(b"a different salt").decode(),
        nonce=sealed.nonce,
        ciphertext=sealed.ciphertext,
        iterations=1000,
    )
    with pytest.raises(vault.VaultError):
        vault.open_sealed(tampered, key)


def test_a_truncated_blob_fails_rather_than_half_decrypting():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    raw = base64.b64decode(sealed.ciphertext)[:-4]
    short = vault.Sealed(
        salt=sealed.salt,
        nonce=sealed.nonce,
        ciphertext=base64.b64encode(raw).decode(),
        iterations=1000,
    )
    with pytest.raises(vault.VaultError):
        vault.open_sealed(short, key)


def test_junk_in_place_of_a_blob_is_refused():
    key = vault.new_key()
    for bad in ("not base64!!", "", "####"):
        sealed = vault.Sealed(salt=bad, nonce=bad, ciphertext=bad, iterations=1000)
        with pytest.raises(vault.VaultError):
            vault.open_sealed(sealed, key)


# -- the properties that make it safe to do twice ------------------------


def test_sealing_the_same_thing_twice_produces_different_blobs():
    """A fresh nonce every time. Reusing one under a single key is how
    AES-GCM fails catastrophically rather than gracefully."""
    key = vault.new_key()
    first = vault.seal(_payload(), key, iterations=1000)
    second = vault.seal(_payload(), key, iterations=1000)
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.salt != second.salt
    # And both still open.
    assert vault.open_sealed(first, key) == vault.open_sealed(second, key)


def test_nonces_never_repeat_across_many_seals():
    key = vault.new_key()
    nonces = {vault.seal({"n": n}, key, iterations=1000).nonce for n in range(300)}
    assert len(nonces) == 300


def test_the_work_factor_is_high_enough_to_matter_by_default():
    """It is what stands between a stolen blob and an offline search."""
    assert vault.ITERATIONS >= 600_000


def test_the_derived_key_is_the_full_width_of_the_cipher():
    assert len(vault.derive("ABCDEFGH", b"salt" * 4, iterations=1000)) == 32


def test_the_same_key_and_salt_always_derive_the_same_thing():
    """Or the phone could never open what the laptop sealed."""
    salt = b"0123456789abcdef"
    first = vault.derive("ABCDEFGH", salt, iterations=1000)
    second = vault.derive("abcdefgh", salt, iterations=1000)
    assert first == second, "case folding must not change the derived key"


def test_an_empty_key_is_refused_rather_than_derived_from():
    with pytest.raises(vault.VaultError):
        vault.derive("!!!", b"salt" * 4, iterations=1000)


def test_a_round_trip_survives_the_wire_format():
    key = vault.new_key()
    sealed = vault.seal(_payload(), key, iterations=1000)
    across = vault.Sealed.from_json(json.loads(json.dumps(sealed.as_json())))
    assert vault.open_sealed(across, key) == _payload()


# -- what actually leaves the laptop ------------------------------------


def _ledger_with_history(tmp_path):
    from datetime import date, timedelta

    from carraway.core import db
    from carraway.core.models import Account, AccountType, Transaction
    from carraway.core.money import Money
    from carraway.ui.data import Ledger

    path = tmp_path / "hist.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Chase Checking 6822", type=AccountType.CHECKING))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="t1",
                account_id="a1",
                date=date.today() - timedelta(days=2),
                amount=Money.parse("-4.50"),
                description="BLUE BOTTLE COFFEE",
                merchant="BLUE BOTTLE COFFEE",
                category="Dining",
            ),
            Transaction(
                id="t2",
                account_id="a1",
                date=date.today() - timedelta(days=400),
                amount=Money.parse("-999.00"),
                description="ANCIENT HISTORY",
                merchant="ANCIENT HISTORY",
            ),
        ],
    )
    conn.close()
    ledger = Ledger(path)
    ledger.load()
    return ledger


def test_the_history_is_bounded_to_a_window(tmp_path):
    """A phone showing three years of statements is not more useful than one
    showing three months, and every row is a row that must be encrypted,
    sent, and stored on a box that does not need it."""
    ledger = _ledger_with_history(tmp_path)
    rows = ledger.pocket_history(days=90)["transactions"]
    assert [r["description"] for r in rows] == ["BLUE BOTTLE COFFEE"]


def test_no_key_means_no_history_leaves_at_all(tmp_path):
    ledger = _ledger_with_history(tmp_path)
    assert ledger.vault_key() is None
    assert ledger.sealed_history() is None


def test_what_is_published_carries_no_readable_history(tmp_path, monkeypatch):
    """The test that matters: whatever goes over the wire must not contain a
    merchant, an amount, or an account name."""
    from carraway.sync import vault
    from carraway.ui.data import Ledger

    ledger = _ledger_with_history(tmp_path)
    key = vault.new_key()
    monkeypatch.setattr(Ledger, "vault_key", lambda self: key)

    sent = {}

    class Client:
        def publish(self, payload):
            sent.update(payload)
            return "2026-09-02T00:00:00+00:00"

    monkeypatch.setattr(Ledger, "pocket_client", lambda self: Client())
    ledger.publish_to_pocket()

    wire = json.dumps(sent)
    assert "history" in sent, "the history was never sent"
    for secret in ("BLUE BOTTLE", "COFFEE", "4.50", "Chase Checking", "6822"):
        assert secret not in wire, f"{secret!r} went over the wire in the clear"

    # And it is the real history underneath, for someone holding the key.
    opened = vault.open_sealed(vault.Sealed.from_json(sent["history"]), key)
    assert opened["transactions"][0]["description"] == "BLUE BOTTLE COFFEE"


def test_rotating_the_key_makes_the_old_history_unreadable(tmp_path):
    """Which is the point of rotating it."""
    from carraway.sync import vault

    ledger = _ledger_with_history(tmp_path)
    sealed = vault.seal(ledger.pocket_history(), "ABCDEFGHJKMNPQRSTVWXYZ0123", iterations=1000)
    with pytest.raises(vault.VaultError):
        vault.open_sealed(sealed, "ZYXWVTSRQPNMKJHGFEDCBA9876")
