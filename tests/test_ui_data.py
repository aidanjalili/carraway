"""The Ledger the screens read from. No Qt needed — this is plain logic."""

from datetime import date, timedelta

from carraway.core import db
from carraway.core.models import Account, AccountType, Transaction
from carraway.core.money import Money
from carraway.importers.csv_importer import import_csv
from carraway.ui.data import Ledger


def _statement(price_after: str) -> str:
    rows = ["Date,Description,Amount"]
    # Eleven months at one price, then four at another: a real price rise.
    for month in range(1, 12):
        rows.append(f"2025-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,-8.43")
    for month in range(1, 5):
        rows.append(f"2026-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,{price_after}")
    return "\n".join(rows) + "\n"


def _ledger(tmp_path, statement: str) -> Ledger:
    import io

    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    txs, _ = import_csv(io.StringIO(statement), "a1")
    db.insert_transactions(conn, txs)
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_current_price_is_used_rather_than_the_median(tmp_path):
    # The median across a price change is the old price, because most of the
    # history sits before it. A view that answers "what am I paying" must not
    # quote a figure the user stopped paying months ago.
    ledger = _ledger(tmp_path, _statement("-9.48"))
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    assert series.typical_amount == Money.parse("-8.43")  # the median
    assert ledger.current_amount(series) == Money.parse("-9.48")  # what they pay now
    assert ledger.current_annual(series) == Money.parse("113.76")  # 9.48 * 12


def test_a_series_with_no_price_change_is_left_alone(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    assert ledger.price_change_for(series) is None
    assert ledger.current_amount(series) == series.typical_amount
    assert ledger.current_annual(series) == series.annualised


def test_transfers_are_matched_without_touching_the_database(tmp_path):
    # Opening the app must never rewrite the user's data behind their back.
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="chk", name="Checking", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="crd", name="Card", type=AccountType.CREDIT_CARD))
    db.insert_transactions(
        conn,
        [
            Transaction(
                "t1",
                "chk",
                date(2026, 1, 25),
                Money.parse("-842.10"),
                "ONLINE PAYMENT TO CARD 4412",
            ),
            Transaction(
                "t2", "crd", date(2026, 1, 26), Money.parse("842.10"), "PAYMENT - THANK YOU"
            ),
        ],
    )
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    assert all(t.is_transfer for t in ledger.transactions)

    # In memory only: a fresh read still shows them ungrouped.
    assert all(not t.transfer_group for t in db.list_transactions(db.connect(path)))


def test_spending_by_category_excludes_money_that_only_moved(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    names = {name for name, _, _ in ledger.spending_by_category()}
    assert "Transfer" not in names


def test_an_account_with_no_balance_is_reported_by_name(tmp_path):
    # accounts_missing_balances returns ids, and an id tells the user nothing.
    # This crashed the net worth screen the first time an account without a
    # recorded balance existed, which is any freshly imported one.
    import io

    from carraway.core.models import Account, AccountType
    from carraway.importers.csv_importer import import_csv

    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Everyday", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="a2", name="No Balance Here", type=AccountType.CASH))
    txs, _ = import_csv(io.StringIO(_statement("-8.43")), "a1")
    db.insert_transactions(conn, txs)
    db.record_balance(conn, "a1", Money.parse("100.00"))
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()

    missing = ledger.accounts_without_balances()
    assert missing == ["a2"]
    by_id = {a.id: a.name for a in ledger.accounts}
    assert [by_id[i] for i in missing] == ["No Balance Here"]


def _tracked_ledger(tmp_path) -> Ledger:
    """A ledger with linked accounts and one tracked subscription on each route."""
    path = tmp_path / "paid.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="wf", name="Wells Fargo Card", type=AccountType.CREDIT_CARD))
    db.upsert_account(conn, Account(id="chk", name="Chase Checking", type=AccountType.CHECKING))
    db.upsert_account(
        conn, Account(id="old", name="Closed Card", type=AccountType.CREDIT_CARD, closed=True)
    )
    db.add_manual_subscription(conn, "Gym", Money.parse("29.54"), "monthly", paid_via_account="wf")
    db.add_manual_subscription(
        conn, "Phone", Money.parse("35.00"), "monthly", paid_via="venmo to dad"
    )
    db.add_manual_subscription(conn, "Domain", Money.parse("15.00"), "yearly")
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def _series_named(ledger: Ledger, name: str):
    return next(s for s in ledger.series if s.merchant == name)


def test_a_linked_account_is_shown_by_its_real_name(tmp_path):
    # The point of storing a reference rather than the text the user typed:
    # the subscription names the account the way every other screen does.
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Gym")) == "Wells Fargo Card"


def test_free_text_survives_for_routes_with_no_account(tmp_path):
    # "venmo to dad" is not an account and never will be. Forcing the answer
    # into a dropdown would make it unrecordable.
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Phone")) == "venmo to dad"


def test_saying_nothing_shows_nothing(tmp_path):
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Domain")) == ""


def test_a_detected_series_reports_the_account_it_was_found_in(tmp_path):
    # No user input needed: the charge landed somewhere, and that is the answer.
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.paid_with(netflix) == "Card"


def test_choosing_an_account_clears_a_stale_note(tmp_path):
    # Both fields are written every time, so the row cannot end up claiming
    # two different answers to one question.
    ledger = _tracked_ledger(tmp_path)
    phone = _series_named(ledger, "Phone")
    assert ledger.set_paid_with(phone, {"paid_via_account": "chk"}) is True
    assert ledger.paid_with(_series_named(ledger, "Phone")) == "Chase Checking"
    entry = ledger.manual_entry(_series_named(ledger, "Phone"))
    assert entry["paid_via"] == ""


def test_a_detected_series_can_be_told_who_really_pays_for_it(tmp_path):
    """The account is a fact; who settles it is a separate question.

    A charge can land on a card that somebody else pays off, or that gets
    reimbursed, and the statement cannot know. So the user's answer wins --
    but as an override, with the observed account still underneath.
    """
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    observed = ledger.paid_with(netflix)

    assert ledger.set_paid_with(netflix, {"paid_via": "dad pays this one"}) is True
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.paid_with(netflix) == "dad pays this one"
    assert ledger.paid_with_is_corrected(netflix) is True
    # The statement's own answer is untouched underneath.
    assert netflix.account_id and ledger.account_name(netflix.account_id) == observed


def test_clearing_the_correction_goes_back_to_the_statement(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    observed = ledger.paid_with(netflix)

    ledger.set_paid_with(netflix, {"paid_via": "dad pays this one"})
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.clear_paid_with(netflix) is True

    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.paid_with(netflix) == observed
    assert ledger.paid_with_is_corrected(netflix) is False


def test_clearing_when_nothing_was_corrected_reports_no_change(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.clear_paid_with(netflix) is False


def test_a_correction_naming_an_account_shows_that_accounts_name(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    other = ledger.accounts[0].id

    ledger.set_paid_with(netflix, {"paid_via_account": other})
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.paid_with(netflix) == ledger.account_name(other)


def test_payable_accounts_puts_cards_first_and_drops_closed_ones(tmp_path):
    ledger = _tracked_ledger(tmp_path)
    offered = ledger.payable_accounts
    assert [a.name for a in offered] == ["Wells Fargo Card", "Chase Checking"]


def _cash_ledger(tmp_path, *, observe: bool = True) -> Ledger:
    """A cash account whose imported history starts long after it opened."""
    path = tmp_path / "cash.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="t1",
                account_id="cash",
                date=date(2026, 6, 10),
                amount=Money.parse("-40.00"),
                description="Market",
            ),
            Transaction(
                id="t2",
                account_id="cash",
                date=date(2026, 8, 5),
                amount=Money.parse("-15.00"),
                description="Coffee",
            ),
        ],
    )
    if observe:
        # Observed after the first transaction and before the second, which is
        # the ordinary case: a statement reaches back further than the reading.
        db.record_balance(conn, "cash", Money.parse("200.00"), date(2026, 7, 1))
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_implied_balance_rolls_the_last_reading_forward(tmp_path):
    # $200 on 1 July, minus the $15 spent after it. The $40 spent in June is
    # already inside the $200 and must not be subtracted twice.
    ledger = _cash_ledger(tmp_path)
    assert ledger.implied_balance("cash") == Money.parse("185.00")


def test_implied_balance_does_not_sum_every_transaction(tmp_path):
    # The bug this guards against: summing all history treats an unknown
    # opening balance as zero. On real data that was wrong by $4,928.
    ledger = _cash_ledger(tmp_path)
    assert ledger.implied_balance("cash") != Money.parse("-55.00")


def test_with_no_reading_at_all_the_transactions_are_all_there_is(tmp_path):
    ledger = _cash_ledger(tmp_path, observe=False)
    assert ledger.implied_balance("cash") == Money.parse("-55.00")


def test_typing_a_balance_records_it_without_touching_history(tmp_path):
    ledger = _cash_ledger(tmp_path)
    before = len(ledger.transactions)
    ledger.set_cash_balance("cash", Money.parse("240.00"), correction=False)
    assert ledger.balances["cash"] == Money.parse("240.00")
    assert len(ledger.transactions) == before  # declined, so nothing invented


def test_a_correction_line_makes_the_history_reach_the_typed_balance(tmp_path):
    # $185 implied, $240 actually there: $55 came in that was never recorded.
    ledger = _cash_ledger(tmp_path)
    gap = ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    assert gap == Money.parse("55.00")

    added = [t for t in ledger.transactions if t.description == "Cash adjustment"]
    assert len(added) == 1
    assert added[0].amount == Money.parse("55.00")
    assert added[0].date == date.today()


def test_a_correction_for_money_quietly_spent_is_negative(tmp_path):
    ledger = _cash_ledger(tmp_path)
    gap = ledger.set_cash_balance("cash", Money.parse("100.00"), correction=True)
    assert gap == Money.parse("-85.00")
    added = next(t for t in ledger.transactions if t.description == "Cash adjustment")
    assert added.amount.minor < 0


def test_correcting_twice_in_a_row_is_a_no_op_the_second_time(tmp_path):
    # After reconciling, the records agree, so there is nothing left to adjust.
    ledger = _cash_ledger(tmp_path)
    ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    gap = ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    assert gap == Money.zero()
    assert len([t for t in ledger.transactions if t.description == "Cash adjustment"]) == 1


def test_a_hand_entered_cash_transaction_lands_in_the_ledger(tmp_path):
    ledger = _cash_ledger(tmp_path)
    assert ledger.add_cash_transaction(
        "cash", date(2026, 8, 20), "Farmers market", Money.parse("-24.00")
    )
    assert any(t.description == "Farmers market" for t in ledger.transactions)


def test_the_same_cash_transaction_twice_is_recorded_once(tmp_path):
    ledger = _cash_ledger(tmp_path)
    args = ("cash", date(2026, 8, 20), "Farmers market", Money.parse("-24.00"))
    assert ledger.add_cash_transaction(*args) is True
    assert ledger.add_cash_transaction(*args) is False


def test_only_cash_accounts_accept_a_typed_balance(tmp_path):
    # Everything else is synced, and a typed figure would be overwritten by
    # the next refresh without saying so.
    ledger = _cash_ledger(tmp_path)
    assert ledger.is_cash_account("cash") is True
    assert ledger.is_cash_account("nope") is False
    assert ledger.is_cash_account(None) is False


def test_renaming_an_account_keeps_its_history(tmp_path):
    ledger = _cash_ledger(tmp_path)
    assert ledger.rename_account("cash", "Pocket money") is True
    assert ledger.account_name("cash") == "Pocket money"
    assert len([t for t in ledger.transactions if t.account_id == "cash"]) == 2
    assert ledger.balances["cash"] == Money.parse("200.00")


def test_a_reading_already_includes_its_own_day(tmp_path):
    # A recorded balance is a closing figure. Counting same-day transactions
    # on top of it double-counts them — on real Venmo data two transfers
    # dated on the reading date would have inflated the balance by $137.
    path = tmp_path / "sameday.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="s1",
                account_id="cash",
                date=date(2026, 8, 29),
                amount=Money.parse("97.00"),
                description="Transfer in",
            )
        ],
    )
    db.record_balance(conn, "cash", Money.parse("1008.47"), date(2026, 8, 29))
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    assert ledger.implied_balance("cash") == Money.parse("1008.47")
    assert ledger.implied_balance("cash") != Money.parse("1105.47")


# -- budgets --------------------------------------------------------------


def _budget_ledger(tmp_path) -> Ledger:
    """Six months of steady spending across two accounts."""
    path = tmp_path / "budget.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="card", name="Card", type=AccountType.CREDIT_CARD))
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    rows = []
    for month in range(2, 8):
        rows.append(
            Transaction(
                id=f"d{month}",
                account_id="card",
                date=date(2026, month, 9),
                amount=Money.parse("-200.00"),
                description="RESTAURANT",
                category="Dining",
            )
        )
        rows.append(
            Transaction(
                id=f"c{month}",
                account_id="cash",
                date=date(2026, month, 12),
                amount=Money.parse("-60.00"),
                description="MARKET",
                category="Groceries",
            )
        )
    db.insert_transactions(conn, rows)
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_a_ledger_starts_with_no_budgets(tmp_path):
    assert _budget_ledger(tmp_path).budgets == []


def test_a_saved_budget_comes_back_on_the_ledger(tmp_path):
    from carraway.analysis.budgets import Budget, Envelope

    ledger = _budget_ledger(tmp_path)
    ledger.save_budget(
        Budget(
            id="b1",
            name="August",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            envelopes=(Envelope("Dining", Money.parse("250")),),
        )
    )
    assert [b.name for b in ledger.budgets] == ["August"]
    assert ledger.budget_by_id("b1").total == Money.parse("250")

    assert ledger.delete_budget("b1") is True
    assert ledger.budgets == []


def test_suggestions_can_be_narrowed_to_some_accounts(tmp_path):
    ledger = _budget_ledger(tmp_path)
    every = {e.category for e in ledger.suggest_envelopes(date(2026, 8, 1), date(2026, 8, 31))}
    card = {
        e.category
        for e in ledger.suggest_envelopes(date(2026, 8, 1), date(2026, 8, 31), accounts=["card"])
    }
    assert "Groceries" in every
    assert "Groceries" not in card
    assert "Dining" in card


def test_budget_status_uses_the_ledgers_own_categories(tmp_path):
    # Not the built-in rules: the budget must agree with what Spending shows.
    from carraway.analysis.budgets import Budget, Envelope

    ledger = _budget_ledger(tmp_path)
    budget = Budget(
        id="b1",
        name="July",
        starts_on=date(2026, 7, 1),
        ends_on=date(2026, 7, 31),
        envelopes=(Envelope("Dining", Money.parse("250")),),
    )
    state = ledger.budget_status(budget, asof=date(2026, 7, 31))
    dining = next(line for line in state.lines if line.category == "Dining")
    assert dining.spent == Money.parse("200")
    assert dining.on_track is True


# -- collecting from the phone --------------------------------------------


def _pocket_ledger(tmp_path) -> Ledger:
    path = tmp_path / "pocket.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


class _StubInbox:
    """Stands in for the server, and records what it was told."""

    def __init__(self, entries):
        self._entries = entries
        self.claimed: list[str] = []
        self.published = None

    def pending(self):
        return self._entries

    def claim(self, ids):
        self.claimed = list(ids)
        return len(ids)

    def publish(self, snapshot):
        self.published = snapshot
        return "2026-08-31T00:00:00+00:00"


def _inbox_entry(**over):
    from carraway.sync.pocket import InboxEntry

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


def test_nothing_happens_when_no_inbox_is_configured(tmp_path):
    ledger = _pocket_ledger(tmp_path)
    assert ledger.collect_from_pocket() == {"configured": False, "added": 0, "unmatched": []}


def test_collecting_stores_the_entry_and_claims_it(tmp_path, monkeypatch):
    ledger = _pocket_ledger(tmp_path)
    stub = _StubInbox([_inbox_entry()])
    monkeypatch.setattr(ledger, "pocket_client", lambda: stub)

    result = ledger.collect_from_pocket()
    assert result["added"] == 1
    assert stub.claimed == ["e1"]
    assert any(t.description == "Farmers market" for t in ledger.transactions)


def test_an_entry_for_an_unknown_account_is_neither_stored_nor_claimed(tmp_path, monkeypatch):
    # It stays on the server so it is not lost while the user works out what
    # the account should be called.
    ledger = _pocket_ledger(tmp_path)
    stub = _StubInbox([_inbox_entry(id="e2", account="Wallet")])
    monkeypatch.setattr(ledger, "pocket_client", lambda: stub)

    result = ledger.collect_from_pocket()
    assert result["added"] == 0
    assert result["unmatched"] == ["Farmers market"]
    assert stub.claimed == []


def test_a_mixed_batch_claims_only_what_was_stored(tmp_path, monkeypatch):
    ledger = _pocket_ledger(tmp_path)
    stub = _StubInbox([_inbox_entry(id="ok"), _inbox_entry(id="bad", account="Wallet")])
    monkeypatch.setattr(ledger, "pocket_client", lambda: stub)

    ledger.collect_from_pocket()
    assert stub.claimed == ["ok"]


def test_collecting_the_same_entry_twice_stores_it_once(tmp_path, monkeypatch):
    # Entries are stored before they are claimed, so a dropped connection
    # means one arrives again. Carraway's dedupe is what makes that safe.
    ledger = _pocket_ledger(tmp_path)
    stub = _StubInbox([_inbox_entry()])
    monkeypatch.setattr(ledger, "pocket_client", lambda: stub)

    ledger.collect_from_pocket()
    second = ledger.collect_from_pocket()
    assert second["added"] == 0
    assert second["skipped"] == 1
    assert sum(1 for t in ledger.transactions if t.description == "Farmers market") == 1


def test_the_snapshot_carries_budget_lines_and_nothing_else(tmp_path, monkeypatch):
    from carraway.analysis.budgets import Budget, Envelope

    ledger = _pocket_ledger(tmp_path)
    ledger.save_budget(
        Budget(
            id="b1",
            name="This month",
            starts_on=date.today(),
            ends_on=date.today() + timedelta(days=20),
            envelopes=(Envelope("Travel", Money.parse("600")),),
        )
    )
    snapshot = ledger.pocket_snapshot()
    (line,) = snapshot["budgets"]
    assert line["category"] == "Travel"
    assert line["remaining"] == "600.00"
    # Nothing that says where the money is, or what was bought.
    assert set(line) == {"category", "allowance", "spent", "remaining", "note"}


# -- a detected series that shares a merchant with a tracked one ---------


def _twinned_ledger(tmp_path) -> Ledger:
    """A detected series with a tracked entry of the same name behind it.

    Detection suppresses the tracked one as a duplicate, so only the detected
    series is on screen -- but the tracked row is where its "paid with" lives.
    """
    ledger = _ledger(tmp_path, _statement("-8.43"))
    conn = db.connect(ledger.path)
    merchant = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper()).merchant
    db.add_manual_subscription(
        conn,
        merchant=merchant,
        amount=Money.parse("-8.43"),
        cadence="monthly",
        kind="subscription",
        paid_via="dad's card",
    )
    conn.close()
    ledger.load()
    return ledger


def _netflix(ledger):
    return next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())


def test_what_the_user_typed_beats_the_account_it_landed_in(tmp_path):
    """The bug: the edit saved to the tracked row, the account won the
    lookup, and so editing "paid with" appeared to do nothing at all."""
    ledger = _twinned_ledger(tmp_path)
    series = _netflix(ledger)
    assert ledger.is_manual(series) is False  # the detected one is on screen
    assert ledger.paid_with(series) == "dad's card"


def test_editing_paid_with_on_such_a_series_actually_shows(tmp_path):
    ledger = _twinned_ledger(tmp_path)
    assert ledger.set_paid_with(_netflix(ledger), {"paid_via": "mum pays it"}) is True
    assert ledger.paid_with(_netflix(ledger)) == "mum pays it"
    assert ledger.paid_with_is_corrected(_netflix(ledger)) is True


def test_the_edit_goes_to_the_series_on_screen_not_the_hidden_twin(tmp_path):
    """Writing to the tracked row would be writing to something else."""
    ledger = _twinned_ledger(tmp_path)
    ledger.set_paid_with(_netflix(ledger), {"paid_via": "mum pays it"})

    conn = db.connect(ledger.path)
    row = conn.execute("SELECT paid_via FROM manual_subscriptions").fetchone()
    conn.close()
    assert row["paid_via"] == "dad's card"  # untouched


def test_undoing_falls_back_to_what_was_typed_before(tmp_path):
    ledger = _twinned_ledger(tmp_path)
    ledger.set_paid_with(_netflix(ledger), {"paid_via": "mum pays it"})
    assert ledger.clear_paid_with(_netflix(ledger)) is True
    assert ledger.paid_with(_netflix(ledger)) == "dad's card"


def test_choosing_an_account_wins_over_the_typed_text(tmp_path):
    ledger = _twinned_ledger(tmp_path)
    ledger.set_paid_with(_netflix(ledger), {"paid_via_account": "a1"})
    assert ledger.paid_with(_netflix(ledger)) == "Card"


# -- suggesting the date a tracked entry last billed ---------------------


def _with_charges(tmp_path, merchant: str, rows: list[tuple[str, str]]) -> Ledger:
    """A tracked entry with no date, plus some statement history."""
    from datetime import date as _d

    path = tmp_path / "suggest.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id=f"s{index}",
                account_id="a1",
                date=_d.fromisoformat(when),
                amount=Money.parse("-8.00"),
                description=description,
                merchant=description,
            )
            for index, (when, description) in enumerate(rows)
        ],
    )
    db.add_manual_subscription(
        conn, merchant=merchant, amount=Money.parse("-8.00"), cadence="monthly"
    )
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_the_most_recent_matching_charge_is_suggested(tmp_path):
    from datetime import date as _d

    # Descriptions that differ, so detection does not group them into a
    # series of its own and suppress the tracked entry as a duplicate.
    ledger = _with_charges(
        tmp_path,
        "The Atlantic",
        [
            ("2025-02-15", "THE ATLANTIC WWW.THEATLANTDC"),
            ("2026-02-15", "THE ATLANTIC MAGAZINE WASHINGTON"),
        ],
    )
    series = next(s for s in ledger.series if s.merchant == "The Atlantic")
    assert ledger.suggest_billed_on(series) == _d(2026, 2, 15)


def test_a_bracketed_aside_is_not_part_of_the_merchant_name(tmp_path):
    from datetime import date as _d

    ledger = _with_charges(
        tmp_path,
        "Patreon (recklessben)",
        [("2026-08-28", "Patreon* Membership Internet CA")],
    )
    series = next(s for s in ledger.series if s.merchant.startswith("Patreon"))
    assert ledger.suggest_billed_on(series) == _d(2026, 8, 28)


def test_a_loose_word_match_is_refused(tmp_path):
    """The real one: "Ava Hollis - Apostle island plus food" was being offered
    as the last charge for "SoundCloud Plus". A confidently wrong date is
    worse than none -- it would be accepted, and then projected from forever.
    """
    ledger = _with_charges(
        tmp_path, "SoundCloud Plus", [("2026-04-14", "Ava Hollis - Apostle island plus food")]
    )
    series = next(s for s in ledger.series if s.merchant == "SoundCloud Plus")
    assert ledger.suggest_billed_on(series) is None


def test_nothing_matching_suggests_nothing(tmp_path):
    ledger = _with_charges(tmp_path, "Grok", [("2026-04-14", "TESCO SUPERSTORE")])
    series = next(s for s in ledger.series if s.merchant == "Grok")
    assert ledger.suggest_billed_on(series) is None


def test_money_coming_in_is_never_a_subscription_charge(tmp_path):
    from carraway.core.models import Transaction as _T

    ledger = _with_charges(tmp_path, "Headspace", [])
    conn = db.connect(ledger.path)
    db.insert_transactions(
        conn,
        [
            _T(
                id="refund",
                account_id="a1",
                date=date(2026, 5, 1),
                amount=Money.parse("70.00"),
                description="HEADSPACE REFUND",
                merchant="HEADSPACE",
            )
        ],
    )
    conn.close()
    ledger.load()
    series = next(s for s in ledger.series if s.merchant == "Headspace")
    assert ledger.suggest_billed_on(series) is None


# -- estimates that say where they came from -----------------------------


def _income_ledger(tmp_path) -> Ledger:
    """Recurring pay, a recurring bill, and one one-off windfall."""
    import io

    rows = ["Date,Description,Amount"]
    for month in range(1, 9):
        rows.append(f"2026-{month:02d}-01,ACME CORP PAYROLL,4000.00")
        rows.append(f"2026-{month:02d}-03,GREAT LANDLORD RENT,-1200.00")
    # A single large deposit, which must not be mistaken for monthly income.
    rows.append("2026-05-20,SOLD THE CAR,9000.00")

    path = tmp_path / "income.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Checking", type=AccountType.CHECKING))
    txs, _ = import_csv(io.StringIO("\n".join(rows) + "\n"), "a1")
    db.insert_transactions(conn, txs)
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_the_income_estimate_says_what_it_counted(tmp_path):
    estimate = _income_ledger(tmp_path).income_estimate()
    assert estimate.known is True
    assert "marked as income" in estimate.source


def test_a_one_off_windfall_is_not_treated_as_income(tmp_path):
    """Budgeting against a car sale plans to sell the car again."""
    ledger = _income_ledger(tmp_path)
    # Only the recurring payroll counts, so the figure stays near one payslip
    # rather than being dragged up by the $9,000.
    assert ledger.income_estimate().amount == Money.parse("4000.00")


def test_no_recurring_income_says_so_instead_of_offering_zero(tmp_path):
    estimate = _ledger(tmp_path, _statement("-8.43")).income_estimate()
    assert estimate.known is False
    assert estimate.confident is False
    assert "type what you expect" in estimate.source.lower()


def test_the_fixed_costs_estimate_names_its_source(tmp_path):
    estimate = _income_ledger(tmp_path).fixed_costs_estimate()
    assert "Carraway knows about" in estimate.source
    # Habits are spending and belong in the table, not in the fixed figure.
    assert "Habits are left out" in estimate.source


def test_nothing_classified_yet_is_reported_rather_than_guessed(tmp_path):
    empty = Ledger(path=tmp_path / "bare.db")
    empty.load()
    estimate = empty.fixed_costs_estimate()
    assert estimate.known is False
    assert estimate.confident is False


def test_the_ledger_hands_back_the_history_basis(tmp_path):
    basis = _income_ledger(tmp_path).history_basis()
    assert basis.months_with_data > 0
    assert "complete month" in basis.describe()


def test_the_basis_follows_the_account_scope(tmp_path):
    # An account with nothing on it has no history to describe.
    assert _income_ledger(tmp_path).history_basis(["nonexistent"]).months_with_data == 0


# -- what counts as committed -------------------------------------------


def test_a_subscription_that_stopped_charging_is_not_a_commitment(tmp_path):
    """A quarterly subscription last charged over a year ago, and overdue ever
    since, was still counted as fixed costs — money the budget then refused to
    let the user spend on anything else. Detection already flags these stale;
    the commitment tally had not asked.
    """
    from datetime import timedelta

    path = tmp_path / "stale.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))

    today = date.today()
    rows = []
    # Alive: three monthly charges, the most recent this month.
    for index in range(3):
        rows.append(
            Transaction(
                id=f"live{index}",
                account_id="a1",
                date=today - timedelta(days=30 * index),
                amount=Money.parse("-20.00"),
                description="LIVE THING",
                merchant="LIVE THING",
                category="Subscriptions",
            )
        )
    # Dead: three monthly charges that stopped over a year ago.
    for index in range(3):
        rows.append(
            Transaction(
                id=f"dead{index}",
                account_id="a1",
                date=today - timedelta(days=400 + 30 * index),
                amount=Money.parse("-50.00"),
                description="DEAD THING",
                merchant="DEAD THING",
                category="Subscriptions",
            )
        )
    db.insert_transactions(conn, rows)
    # Both are subscriptions as far as the user is concerned. Without saying
    # so they come back "unknown", which is not a committed kind, and the test
    # would pass for the wrong reason.
    db.set_verdict(conn, "LIVE THING", "subscription")
    db.set_verdict(conn, "DEAD THING", "subscription")
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()

    stale = {s.merchant for s in ledger.stale_series}
    assert any("DEAD" in name.upper() for name in stale), "the dead one was not flagged stale"

    committed = ledger.committed_by_category()
    # Asserted on the total rather than a named bucket: which category a
    # commitment lands in is decided by the categorisation rules, and this is
    # a test about what counts as committed at all.
    total = sum(abs(amount.minor) for amount in committed.values())
    assert total == Money.parse("20.00").minor, (
        f"expected only the live subscription, got {committed}"
    )


def _lapsed_statement() -> str:
    """A monthly subscription that charged for a year and then simply stopped."""
    rows = ["Date,Description,Amount"]
    for month in range(1, 13):
        rows.append(f"2025-{month:02d}-05,SPOTIFY USA,-11.99")
    # A second, still-live subscription, so the ledger is not entirely stale.
    for month in range(1, 13):
        rows.append(f"2025-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,-8.43")
    for month in range(1, 9):
        rows.append(f"2026-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,-8.43")
    return "\n".join(rows) + "\n"


def test_stale_series_leave_both_committed_figures_alike(tmp_path):
    """The headline fixed-cost figure and the per-category breakdown must agree.

    committed_by_category has skipped stale series since a quarterly
    subscription last charged in May 2025 was found still claiming $16 a month
    of someone's budget. committed_per_month never got the same treatment, so
    the two disagreed by exactly the cost of whatever had quietly stopped
    billing -- and the headline number, the one offered to the user as "your
    fixed costs are...", was the wrong one of the pair. Money reserved for a
    subscription that stopped billing is money the user is told they may not
    spend.
    """
    ledger = _ledger(tmp_path, _lapsed_statement())

    # Assert the fixture first: with nothing stale this test proves nothing,
    # and would keep passing after the guard it exists to protect was removed.
    assert len(ledger.stale_series) == 1, "fixture stopped producing a stale series"
    lapsed = ledger.current_amount(ledger.stale_series[0])
    assert lapsed.minor != 0, "a stale series costing nothing cannot show the bug"

    by_category = sum(m.minor for m in ledger.committed_by_category().values())
    assert ledger.committed_per_month().minor == by_category
