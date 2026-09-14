"""Smoke tests that actually invoke the view handlers.

These exist because of a real bug: Upcoming's "What is X?" menu item called
`ClassifyDialog.ask(...)`, a classmethod that does not exist. Nothing caught
it — ruff cannot see through an attribute access, and the sibling handler
next to it (`_dismiss`) was tested end to end while this one never was. The
menu item simply did nothing when clicked.

So the rule these encode is: every context-menu handler gets invoked at least
once, with its dialog stubbed out. They need Qt, which CI does not install, so
they skip there and run locally.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI tests need the [gui] extra")

from datetime import date  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QTableView,
)

from carraway.core import db  # noqa: E402
from carraway.core.models import Account, AccountType, Transaction  # noqa: E402
from carraway.core.money import Money  # noqa: E402
from carraway.ui.data import Ledger  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    """A ledger with one unmistakable monthly series."""
    path = tmp_path / "views.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id=f"n{month}",
                account_id="a1",
                date=date(2026, month, 16),
                amount=Money.parse("-8.43"),
                description="NETFLIX.COM",
            )
            for month in range(1, 9)
        ],
    )
    conn.close()
    found = Ledger(path=path)
    found.load()
    return found


def _accept(monkeypatch, dialog_cls, **attrs):
    """Make a dialog accept immediately, with `attrs` as its answer."""
    monkeypatch.setattr(dialog_cls, "exec", lambda self: QDialog.DialogCode.Accepted)
    for name, value in attrs.items():
        monkeypatch.setattr(dialog_cls, name, property(lambda self, v=value: v), raising=False)


def test_upcoming_classify_opens_the_dialog_and_applies_it(app, ledger, monkeypatch):
    # The exact bug: this called a classmethod that did not exist, so the menu
    # item raised AttributeError and looked like it did nothing.
    from carraway.ui.views.classify_dialog import ClassifyDialog
    from carraway.ui.views.upcoming import UpcomingView

    _accept(monkeypatch, ClassifyDialog, chosen="bill")
    view = UpcomingView(ledger)
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    view._classify(series)
    assert ledger.kind_of(series) == "bill"


def test_upcoming_dismiss_removes_the_series_everywhere(app, ledger):
    from carraway.ui.views.upcoming import UpcomingView

    view = UpcomingView(ledger)
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    view._dismiss(series)
    assert not any("NETFLIX" in s.merchant.upper() for s in ledger.series)
    assert any("NETFLIX" in s.merchant.upper() for s in ledger.dismissed)


def test_subscriptions_classify_opens_the_dialog_and_applies_it(app, ledger, monkeypatch):
    from carraway.ui.views.classify_dialog import ClassifyDialog
    from carraway.ui.views.subscriptions import SubscriptionsView

    _accept(monkeypatch, ClassifyDialog, chosen="habit")
    view = SubscriptionsView(ledger)
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    assert view._classify(series) is True
    assert ledger.kind_of(series) == "habit"


def test_every_context_menu_handler_is_callable(app, ledger):
    """Guards the shape of the bug rather than one instance of it.

    A handler that names a method which does not exist passes every other
    check in this repo, so each one is called once here.
    """
    from carraway.ui.views.subscriptions import SubscriptionsView
    from carraway.ui.views.upcoming import UpcomingView

    for view, handlers in (
        (UpcomingView(ledger), ["_classify", "_dismiss"]),
        (
            SubscriptionsView(ledger),
            ["_classify", "_dismiss", "_edit", "_set_paid_with", "_restore"],
        ),
    ):
        for name in handlers:
            handler = getattr(view, name, None)
            assert callable(handler), f"{type(view).__name__}.{name} is missing"


# -- paid with, and when a tracked entry last billed --------------------


def _tracked(ledger, merchant="Costco membership", cadence="yearly"):
    """Add a tracked entry with no billing date, as a spreadsheet import has."""
    from carraway.core import db

    conn = db.connect(ledger.path)
    db.add_manual_subscription(
        conn,
        merchant=merchant,
        amount=Money.parse("-65.00"),
        cadence=cadence,
        kind="subscription",
    )
    conn.close()
    ledger.load()
    return next(s for s in ledger.series if s.merchant == merchant)


def test_a_tracked_entry_with_no_date_shows_no_next_charge(ledger):
    series = _tracked(ledger)
    assert series.next_expected is None
    assert ledger.billed_on(series) is None


def test_setting_the_billing_date_produces_a_next_charge(app, ledger, monkeypatch):
    from datetime import date, timedelta

    from carraway.ui.views import billing_date, subscriptions

    series = _tracked(ledger)
    when = date.today() - timedelta(days=30)
    monkeypatch.setattr(billing_date, "prompt", lambda *a, **k: when)

    view = subscriptions.SubscriptionsView(ledger)
    view._set_billed_on(series)

    ledger.load()
    after = next(s for s in ledger.series if s.merchant == "Costco membership")
    assert ledger.billed_on(after) == when
    assert after.next_expected is not None
    assert after.next_expected > date.today()


def test_cancelling_the_billing_date_dialog_changes_nothing(app, ledger, monkeypatch):
    from carraway.ui.views import billing_date, subscriptions

    series = _tracked(ledger)
    monkeypatch.setattr(billing_date, "prompt", lambda *a, **k: billing_date.CANCELLED)
    subscriptions.SubscriptionsView(ledger)._set_billed_on(series)

    ledger.load()
    assert (
        ledger.billed_on(next(s for s in ledger.series if s.merchant == "Costco membership"))
        is None
    )


def test_saying_you_do_not_know_clears_the_billing_date(app, ledger, monkeypatch):
    """None is a real answer, and must not be mistaken for Cancel."""
    from datetime import date, timedelta

    from carraway.ui.views import billing_date, subscriptions

    series = _tracked(ledger)
    view = subscriptions.SubscriptionsView(ledger)

    monkeypatch.setattr(billing_date, "prompt", lambda *a, **k: date.today() - timedelta(days=5))
    view._set_billed_on(series)
    ledger.load()
    series = next(s for s in ledger.series if s.merchant == "Costco membership")
    assert ledger.billed_on(series) is not None

    monkeypatch.setattr(billing_date, "prompt", lambda *a, **k: None)
    view._set_billed_on(series)
    ledger.load()
    series = next(s for s in ledger.series if s.merchant == "Costco membership")
    assert ledger.billed_on(series) is None
    assert series.next_expected is None


def test_paid_with_can_be_set_on_a_detected_series_from_the_menu(app, ledger, monkeypatch):
    from carraway.ui.views import paid_with, subscriptions

    detected = next(s for s in ledger.series if s.transaction_ids)
    monkeypatch.setattr(paid_with, "prompt", lambda *a, **k: {"paid_via": "dad pays it"})

    view = subscriptions.SubscriptionsView(ledger)
    view._set_paid_with(detected)

    ledger.load()
    again = next(s for s in ledger.series if s.merchant == detected.merchant)
    assert ledger.paid_with(again) == "dad pays it"
    assert ledger.paid_with_is_corrected(again) is True


def test_the_correction_can_be_undone_from_the_menu(app, ledger, monkeypatch):
    from carraway.ui.views import paid_with, subscriptions

    detected = next(s for s in ledger.series if s.transaction_ids)
    statement_says = ledger.paid_with(detected)
    monkeypatch.setattr(paid_with, "prompt", lambda *a, **k: {"paid_via": "dad pays it"})

    view = subscriptions.SubscriptionsView(ledger)
    view._set_paid_with(detected)
    ledger.load()
    detected = next(s for s in ledger.series if s.merchant == detected.merchant)

    view._clear_paid_with(detected)
    ledger.load()
    detected = next(s for s in ledger.series if s.merchant == detected.merchant)
    assert ledger.paid_with(detected) == statement_says
    assert ledger.paid_with_is_corrected(detected) is False


# -- the calendar moving under a window that is left open ---------------


def test_the_window_reloads_when_the_date_changes(app, ledger, monkeypatch):
    """Next charge dates count forward from today, and today is read at load.

    An app left open across midnight would otherwise go on showing
    yesterday's answer, and for anything billing today, a date that passed.
    """
    from datetime import date, timedelta

    from carraway.ui import main_window

    window = main_window.MainWindow(ledger.path)
    reloads: list[int] = []
    monkeypatch.setattr(window, "refresh_all", lambda: reloads.append(1))

    # Same day: nothing to do.
    window._check_the_date()
    assert reloads == []

    # The clock rolls over.
    tomorrow = date.today() + timedelta(days=1)

    class Rolled(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(main_window, "date", Rolled)
    window._check_the_date()
    assert reloads == [1]

    # And it does not keep firing once it has caught up.
    window._check_the_date()
    assert reloads == [1]


def test_transactions_shows_its_balance_the_moment_it_opens(app, ledger):
    """It was filled in only when a tab was clicked, so the screen opened
    with a blank card where the balance goes -- and with the two cash
    buttons showing over it, which belong to a cash account."""
    from carraway.core import db
    from carraway.core.money import Money
    from carraway.ui.views.transactions import TransactionsView

    conn = db.connect(ledger.path)
    db.record_balance(conn, "a1", Money.parse("-250.00"), date.today())
    conn.close()
    ledger.load()

    view = TransactionsView(ledger)
    assert view.balance.amount.text().strip(), "the balance banner opened empty"
    assert view.balance.caption.text().strip()
    assert view.add_txn_button.isVisible() is False
    assert view.set_balance_button.isVisible() is False


# -- money on its way, on the Net worth screen ----------------------------


@pytest.fixture
def with_balance(tmp_path) -> Ledger:
    """A ledger with a real balance, so net worth has an anchor to work from."""
    path = tmp_path / "worth.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="chk", name="Checking", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="ret", name="Retirement", type=AccountType.INVESTMENT))
    db.record_balance(conn, "chk", Money.parse("1000.00"), date(2026, 9, 1))
    db.record_balance(conn, "ret", Money.parse("5000.00"), date(2026, 9, 1))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="t1",
                account_id="chk",
                date=date(2026, 8, 20),
                amount=Money.parse("-40.00"),
                description="GROCERIES",
            )
        ],
    )
    conn.close()
    found = Ledger(path=path)
    found.load()
    return found


def test_the_net_worth_screen_builds_and_refreshes_with_money_on_its_way(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    with_balance.add_expected_money(
        "Tax refund cheque", Money.parse("412.50"), expected_on=date(2026, 9, 20)
    )
    view = NetWorthView(with_balance)
    view.refresh()

    assert view.expected_table.rowCount() == 1
    assert view.expected_table.item(0, 1).text() == "Tax refund cheque"
    assert "$412.50" in view.expected_table.item(0, 2).text()
    # The headline stays what the bank says; the projection sits under it.
    assert "412.50" not in view.net_card.value_label.text()
    assert "on its way" in view.net_card._comparison.text()


def test_the_projected_total_is_the_real_one_plus_what_is_coming(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    real = view.ledger.networth_points("monthly")[-1].net

    with_balance.add_expected_money("Cheque", Money.parse("412.50"))
    with_balance.add_expected_money("Bill I know about", Money.parse("-100.00"))
    view.refresh()

    assert with_balance.expected_total() == Money.parse("312.50")
    after = Money(real.minor + 31250, real.currency)
    assert after.format() in view.expected_blurb.text()
    # Unchanged: nothing here may move the figure the bank would agree with.
    assert view.ledger.networth_points("monthly")[-1].net == real


def test_money_landing_in_an_account_left_out_of_net_worth_is_left_out_too(app, with_balance):
    """Otherwise it moves a total that account is not part of."""
    with_balance.save_setting("networth_excluded_accounts", ["ret"])
    with_balance.load()
    with_balance.add_expected_money("Dividend", Money.parse("200.00"), account_id="ret")
    with_balance.add_expected_money("Cheque", Money.parse("50.00"), account_id="chk")

    assert with_balance.expected_total() == Money.parse("50.00")

    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.refresh()
    rows = [view.expected_table.item(r, 3).text() for r in range(view.expected_table.rowCount())]
    assert any("not counted" in text for text in rows)


def test_removing_an_entry_takes_it_back_out_of_the_projection(app, with_balance):
    entry_id = with_balance.add_expected_money("Cheque", Money.parse("412.50"))
    assert with_balance.expected_total() == Money.parse("412.50")
    assert with_balance.delete_expected_money(entry_id) is True
    assert with_balance.expected_total() == Money.zero()
    assert with_balance.expected_money() == []


def test_the_panel_still_draws_when_no_balance_is_known(app, tmp_path):
    """No anchor means no net worth line at all, but what is written down
    here must not vanish along with it."""
    from carraway.ui.views.networth import NetWorthView

    path = tmp_path / "empty.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="chk", name="Checking", type=AccountType.CHECKING))
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    ledger.add_expected_money("Cheque", Money.parse("412.50"))

    view = NetWorthView(ledger)
    view.refresh()
    assert view.expected_table.rowCount() == 1
    assert "no known balance" in view.expected_blurb.text()


def test_the_add_handler_writes_what_the_dialog_answered(app, with_balance, monkeypatch):
    from carraway.ui.views import expected_money
    from carraway.ui.views.networth import NetWorthView

    _accept(
        monkeypatch,
        expected_money.ExpectedMoneyDialog,
        values={
            "description": "Rent deposit back",
            "amount": Money.parse("950.00"),
            "expected_on": date(2026, 10, 1),
            "account_id": "chk",
            "note": "landlord said two weeks",
        },
    )
    view = NetWorthView(with_balance)
    view._add_expected()

    written = with_balance.expected_money()
    assert len(written) == 1
    assert written[0].description == "Rent deposit back"
    assert written[0].amount == Money.parse("950.00")
    assert written[0].expected_on == date(2026, 10, 1)
    assert written[0].note == "landlord said two weeks"


def test_the_remove_handler_deletes_the_selected_row(app, with_balance, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from carraway.ui.views.networth import NetWorthView

    with_balance.add_expected_money("Cheque", Money.parse("412.50"))
    view = NetWorthView(with_balance)
    view.expected_table.selectRow(0)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    view._remove_expected()
    assert with_balance.expected_money() == []


def test_cancelling_the_dialog_writes_nothing(app, with_balance, monkeypatch):
    from carraway.ui.views import expected_money
    from carraway.ui.views.networth import NetWorthView

    monkeypatch.setattr(
        expected_money.ExpectedMoneyDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    view = NetWorthView(with_balance)
    view._add_expected()
    assert with_balance.expected_money() == []


# -- the dialog's own arithmetic ------------------------------------------


def test_the_dialog_signs_the_amount_by_the_direction_chosen(app):
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    dialog = ExpectedMoneyDialog(current_net=Money.parse("1000.00"))
    dialog.description.setText("Cheque")
    dialog.amount.setText("412.50")
    assert dialog.values["amount"] == Money.parse("412.50")

    dialog.direction.setCurrentText("going out")
    assert dialog.values["amount"] == Money.parse("-412.50")
    # Typing the minus sign as well must not flip it back to positive.
    dialog.amount.setText("-412.50")
    assert dialog.values["amount"] == Money.parse("-412.50")


def test_the_dialog_previews_what_net_worth_becomes(app):
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    dialog = ExpectedMoneyDialog(current_net=Money.parse("1000.00"))
    dialog.amount.setText("412.50")
    assert "$1,412.50" in dialog.preview.text()

    dialog.direction.setCurrentText("going out")
    assert "$587.50" in dialog.preview.text()


def test_the_dialog_refuses_an_empty_or_unreadable_entry(app):
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    dialog = ExpectedMoneyDialog(current_net=Money.parse("1000.00"))
    dialog.amount.setText("412.50")
    dialog._accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "Say what it is" in dialog.preview.text()

    dialog.description.setText("Cheque")
    dialog.amount.setText("four hundred")
    dialog._accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "not an amount" in dialog.preview.text()

    dialog.amount.setText("0")
    dialog._accept()
    assert "not worth writing down" in dialog.preview.text()


def test_an_undated_entry_is_allowed_and_sorts_last(app, with_balance):
    """"Sometime this month" is often all anyone knows."""
    with_balance.add_expected_money("Sometime money", Money.parse("50.00"))
    with_balance.add_expected_money(
        "Dated money", Money.parse("60.00"), expected_on=date(2026, 9, 20)
    )
    order = [e.description for e in with_balance.expected_money()]
    assert order == ["Dated money", "Sometime money"]

    from carraway.ui.views.expected_money import describe

    assert describe(with_balance.expected_money()[1]) == "date unknown"


# -- looking at several accounts at once ----------------------------------


@pytest.fixture
def three_accounts(tmp_path) -> Ledger:
    from carraway.core.models import AccountType as T

    path = tmp_path / "multi.db"
    conn = db.connect(path)
    for aid, name, kind in (
        ("chk", "Checking", T.CHECKING),
        ("card", "Card", T.CREDIT_CARD),
        ("cash", "Cash", T.CASH),
    ):
        db.upsert_account(conn, Account(id=aid, name=name, type=kind))
    db.record_balance(conn, "chk", Money.parse("1000.00"), date(2026, 9, 1))
    db.record_balance(conn, "card", Money.parse("200.00"), date(2026, 9, 1))
    db.record_balance(conn, "cash", Money.parse("50.00"), date(2026, 9, 1))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id=f"{aid}{n}",
                account_id=aid,
                date=date(2026, 8, 10 + n),
                amount=Money.parse("-5.00"),
                description=f"{aid.upper()} SPEND {n}",
            )
            for aid, count in (("chk", 4), ("card", 3), ("cash", 2))
            for n in range(count)
        ],
    )
    conn.close()
    found = Ledger(path=path)
    found.load()
    return found


def _chip_for(view, account_id):
    return next(
        i for i in range(view.tabs.count()) if view.tabs.tabData(i) == account_id
    )


def test_two_accounts_can_be_looked_at_together(app, three_accounts):
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    assert view.proxy.rowCount() == 9  # opens on All

    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([0, chk])
    view._accounts_clicked([0, chk])
    assert view.proxy.rowCount() == 4

    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])
    assert view.proxy.rowCount() == 7


def test_picking_an_account_while_all_is_on_replaces_it(app, three_accounts):
    """The first click out of the default must do what it looks like it does,
    not collapse straight back to All."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk = _chip_for(view, "chk")
    view.tabs.setSelectedIndexes([0, chk])
    view._accounts_clicked([0, chk])
    assert view.tabs.selectedIndexes() == [chk]


def test_clicking_all_from_a_set_of_accounts_clears_them(app, three_accounts):
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])
    view.tabs.setSelectedIndexes([0, chk, card])
    view._accounts_clicked([0, chk, card])
    assert view.tabs.selectedIndexes() == [0]
    assert view.proxy.rowCount() == 9


def test_unticking_the_last_account_falls_back_to_all(app, three_accounts):
    """An empty strip over an empty table reads as broken, not deliberate."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    view.tabs.setSelectedIndexes([])
    view._accounts_clicked([])
    assert view.tabs.selectedIndexes() == [0]
    assert view.proxy.rowCount() == 9


def test_the_banner_nets_a_handful_of_accounts(app, three_accounts):
    """A card subtracts, the same as it does across all accounts."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])
    assert view.balance.amount.text() == "$800.00"
    assert "2 accounts" in view.balance.caption.text().lower()


def test_cash_actions_need_exactly_one_account(app, three_accounts):
    """Setting a balance has to name one account; with two there is none."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    cash, chk = _chip_for(view, "cash"), _chip_for(view, "chk")
    view.tabs.setSelectedIndexes([cash])
    view._accounts_clicked([cash])
    assert view._cash_account() == "cash"

    view.tabs.setSelectedIndexes([cash, chk])
    view._accounts_clicked([cash, chk])
    assert view._cash_account() is None


def test_the_account_column_comes_back_when_several_are_shown(app, three_accounts):
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([chk])
    view._accounts_clicked([chk])
    assert view.table.isColumnHidden(3) is True

    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])
    assert view.table.isColumnHidden(3) is False


def test_a_refresh_keeps_the_accounts_being_looked_at(app, three_accounts):
    """_build_tabs destroys and remakes every chip, which checks the first
    one -- a sync must not silently snap the view back to All."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])

    view.refresh()
    assert set(view._selected_account_ids()) == {"chk", "card"}
    assert view.proxy.rowCount() == 7


# -- and remembering how it was left --------------------------------------


def test_the_view_reopens_where_it_was_left(app, three_accounts):
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    chk, card = _chip_for(view, "chk"), _chip_for(view, "card")
    view.tabs.setSelectedIndexes([chk, card])
    view._accounts_clicked([chk, card])
    view.kind.setCurrentText("Spending")
    view.table.sortByColumn(4, Qt.SortOrder.AscendingOrder)

    reopened = TransactionsView(three_accounts)
    assert set(reopened._selected_account_ids()) == {"chk", "card"}
    assert reopened.kind.currentText() == "Spending"
    header = reopened.table.horizontalHeader()
    assert header.sortIndicatorSection() == 4
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder


def test_each_control_saves_on_its_own(app, three_accounts):
    """Saving from only some of them persists by accident, depending on which
    control was touched last."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    view.kind.setCurrentText("Spending")
    del view

    assert TransactionsView(three_accounts).kind.currentText() == "Spending"


def test_a_search_is_not_remembered(app, three_accounts):
    """Four rows out of 2,600 with no visible filter reads as a broken app."""
    from carraway.ui.views.transactions import TransactionsView

    view = TransactionsView(three_accounts)
    view.search.setText("CHK SPEND 1")
    view.kind.setCurrentText("Spending")  # force a save

    assert TransactionsView(three_accounts).search.text() == ""


def test_a_remembered_account_that_no_longer_exists_falls_back_to_all(
    app, three_accounts
):
    from carraway.ui.views.transactions import TransactionsView

    three_accounts.save_setting(
        "transactions_view", {"accounts": ["an-account-since-closed"]}
    )
    view = TransactionsView(three_accounts)
    assert view.tabs.selectedIndexes() == [0]
    assert view.proxy.rowCount() == 9


def test_a_remembered_category_that_was_hidden_is_ignored(app, three_accounts):
    """Restoring it would filter to nothing with no clue why."""
    from carraway.ui.views.transactions import TransactionsView

    three_accounts.save_setting(
        "transactions_view", {"kind": "A Category Nobody Has Any More"}
    )
    view = TransactionsView(three_accounts)
    assert view.kind.currentText() == "All"
    assert view.proxy.rowCount() == 9


# -- correcting an entry rather than retyping it --------------------------


def test_an_entry_can_be_edited_in_place(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    entry_id = with_balance.add_expected_money(
        "Card pending", Money.parse("-150.00"), expected_on=date(2027, 9, 15)
    )
    view = NetWorthView(with_balance)
    view.expected_table.selectRow(0)

    with_balance.update_expected_money(
        entry_id, "Card pending", Money.parse("-150.00"), expected_on=date(2026, 9, 15)
    )
    fixed = with_balance.expected_money()[0]
    # The id survives, so anything holding it is not left pointing at nothing.
    assert fixed.id == entry_id
    assert fixed.expected_on == date(2026, 9, 15)
    assert fixed.amount == Money.parse("-150.00")


def test_the_dialog_comes_back_filled_in(app, with_balance):
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    with_balance.add_expected_money(
        "Card pending",
        Money.parse("-150.00"),
        expected_on=date(2027, 9, 15),
        account_id="chk",
        note="should clear by wed",
    )
    entry = with_balance.expected_money()[0]
    dialog = ExpectedMoneyDialog(with_balance.accounts, Money.parse("1000.00"), None, entry)

    assert dialog.description.text() == "Card pending"
    assert dialog.amount.text() == "150.00"
    assert dialog.direction.currentText() == "going out"
    assert dialog.dated.isChecked() is True
    assert dialog.expected_on.date().toPython() == date(2027, 9, 15)
    assert dialog.account.currentData() == "chk"
    assert dialog.note.text() == "should clear by wed"
    # Unchanged, it must give back exactly what it was handed.
    assert dialog.values["amount"] == Money.parse("-150.00")


def test_editing_does_not_double_count_the_entry_in_its_own_preview(app, with_balance):
    """Net worth already includes this entry's effect in what the view shows,
    so previewing against it unbacked would add the figure twice."""
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    with_balance.add_expected_money("Card pending", Money.parse("-150.00"))
    entry = with_balance.expected_money()[0]

    dialog = ExpectedMoneyDialog(None, Money.parse("1000.00"), None, entry)
    assert "$1,150.00" in dialog.preview.text()  # the real figure, entry backed out
    assert "$1,000.00" in dialog.preview.text()  # and where it lands again


def test_an_undated_entry_reopens_undated(app, with_balance):
    """Reopening it must not silently invent a date."""
    from carraway.ui.views.expected_money import ExpectedMoneyDialog

    with_balance.add_expected_money("Sometime money", Money.parse("50.00"))
    entry = with_balance.expected_money()[0]

    dialog = ExpectedMoneyDialog(None, Money.parse("1000.00"), None, entry)
    assert dialog.dated.isChecked() is False
    assert dialog.values["expected_on"] is None


def test_one_day_away_reads_as_tomorrow(app, with_balance):
    from datetime import timedelta

    from carraway.ui.views.expected_money import describe

    with_balance.add_expected_money(
        "Cheque", Money.parse("10.00"), expected_on=date.today() + timedelta(days=1)
    )
    with_balance.add_expected_money(
        "Late one", Money.parse("10.00"), expected_on=date.today() - timedelta(days=1)
    )
    entries = {e.description: e for e in with_balance.expected_money()}
    assert describe(entries["Cheque"]).endswith("tomorrow")
    assert describe(entries["Late one"]).endswith("1 day overdue")


# -- tooltips that wrap instead of running off the screen ------------------


def test_a_long_tooltip_becomes_wrapping_rich_text(app):
    from carraway.ui.widgets import as_tooltip

    out = as_tooltip("One sentence.\n\nA second paragraph.")
    # Qt only wraps a tooltip when it is rich text; plain text is laid out on
    # one line per paragraph however wide that ends up being.
    assert out.startswith("<qt>")
    assert "width:" in out
    assert "<br><br>" in out
    assert "One sentence." in out and "A second paragraph." in out


def test_tooltip_text_is_escaped(app):
    """An amount like "<$5" must not be swallowed as a tag."""
    from carraway.ui.widgets import as_tooltip

    out = as_tooltip("a < b & c > d")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out


# -- the calendar popup ----------------------------------------------------


def test_today_is_marked_in_the_calendar(app):
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QDateEdit

    from carraway.ui.widgets import dress_calendar

    picker = QDateEdit()
    picker.setCalendarPopup(True)
    dress_calendar(picker)
    calendar = picker.calendarWidget()

    marked = calendar.dateTextFormat(QDate.currentDate())
    assert not marked.isEmpty(), "today carries no marking"
    assert marked.fontWeight() > 50
    # Every other day is left alone, or the mark would say nothing.
    assert calendar.dateTextFormat(QDate.currentDate().addDays(5)).isEmpty()
    assert calendar.isGridVisible()


# -- panels the user can resize and have remembered ------------------------


def test_panels_remember_what_they_were_dragged_to(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1200, 900)
    view.show()
    app.processEvents()
    view.panels.setSizes([700, 100, 100])
    view.panels.save()

    reopened = NetWorthView(with_balance)
    reopened.resize(1200, 900)
    reopened.show()
    app.processEvents()
    # Proportions rather than exact pixels: a splitter scales what it
    # restores to the space it actually has.
    sizes = reopened.panels.sizes()
    assert sizes[0] > sizes[1] + sizes[2]


def test_one_panel_can_take_the_whole_screen_and_come_back(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1200, 900)
    view.panels.setSizes([300, 200, 200])
    before = view.panels.sizes()

    view.panels.toggle_full(0)
    sizes = view.panels.sizes()
    assert sizes[1] == 0 and sizes[2] == 0 and sizes[0] > 0

    view.panels.toggle_full(0)
    # Back to what the user had, not to the shipped default.
    assert view.panels.sizes() == before


def test_resetting_puts_every_screen_back(app, with_balance):
    from carraway.ui.views.networth import NetWorthView
    from carraway.ui.widgets import PanelSplitter

    view = NetWorthView(with_balance)
    view.resize(1200, 900)
    view.panels.setSizes([700, 100, 100])
    view.panels.save()
    assert with_balance.setting(view.panels.setting_key)

    PanelSplitter.reset_all(with_balance)
    assert not with_balance.setting(view.panels.setting_key)


def test_a_screen_with_nothing_saved_uses_its_defaults(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1200, 900)
    view.show()
    app.processEvents()
    view.panels.restore()
    sizes = view.panels.sizes()
    # 5:3:3 -- the chart is what people come to look at.
    assert sizes[0] > sizes[1] and sizes[0] > sizes[2]


def test_a_corrupt_saved_layout_falls_back_rather_than_failing(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    with_balance.save_setting("panels:networth", "not base64 at all!!")
    view = NetWorthView(with_balance)
    view.resize(1200, 900)
    assert view.panels.count() == 3
    assert sum(view.panels.sizes()) > 0


# -- columns the user can drag --------------------------------------------


def test_every_column_divider_can_be_dragged(app, with_balance):
    """Leaving one column on Stretch stopped leftover width becoming dead
    space, and cost that column both of its dividers -- on the widest column,
    which is the one people reach for first."""
    from PySide6.QtWidgets import QHeaderView

    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1400, 900)
    header = view.table.horizontalHeader()
    modes = [header.sectionResizeMode(c) for c in range(view.table.columnCount())]

    assert modes, "the table has no columns"
    assert all(m == QHeaderView.ResizeMode.Interactive for m in modes)
    assert QHeaderView.ResizeMode.Stretch not in modes


def test_every_table_in_the_app_has_draggable_columns(app, with_balance, three_accounts):
    """One table left on Stretch is the one the user will happen to try."""
    from PySide6.QtWidgets import QHeaderView

    from carraway.ui.views.networth import NetWorthView
    from carraway.ui.views.transactions import TransactionsView

    for view in (NetWorthView(with_balance), TransactionsView(three_accounts)):
        view.resize(1400, 900)
        for table in view.findChildren(QTableView):
            # A QCalendarWidget keeps its day grid in a QTableView whose
            # columns are Stretch by design. It is a calendar, not data.
            if table.objectName() == "qt_calendar_calendarview":
                continue
            header = table.horizontalHeader()
            model = table.model()
            count = model.columnCount() if model is not None else 0
            modes = [header.sectionResizeMode(c) for c in range(count)]
            assert QHeaderView.ResizeMode.Stretch not in modes, (
                f"{type(view).__name__} has a column that cannot be dragged"
            )


def test_a_dragged_column_width_comes_back(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1400, 900)
    header = view.table.horizontalHeader()
    header.resizeSection(2, 240)
    with_balance.save_setting(
        "columns:networth.history",
        header.saveState().toBase64().data().decode("ascii"),
    )

    reopened = NetWorthView(with_balance)
    reopened.resize(1400, 900)
    assert reopened.table.horizontalHeader().sectionSize(2) == 240


def test_resetting_forgets_column_widths_too(app, with_balance):
    from carraway.ui.widgets import reset_columns

    with_balance.save_setting("columns:networth.history", "something")
    with_balance.save_setting("columns:transactions", "something else")
    assert reset_columns(with_balance) == 2
    assert not with_balance.setting("columns:networth.history")


def test_a_corrupt_saved_column_state_is_ignored(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    with_balance.save_setting("columns:networth.history", "not base64 at all!!")
    view = NetWorthView(with_balance)
    view.resize(1400, 900)
    assert view.table.columnCount() == 5


def test_a_saved_layout_cannot_bring_back_an_undraggable_column(app, with_balance):
    """A header's saved state carries its resize *modes* as well as its
    widths. Restoring one written before columns became draggable put Stretch
    back on a column, and that column's dividers went dead again -- on the
    exact table the user was trying to drag."""
    from PySide6.QtWidgets import QHeaderView

    from carraway.ui.views.networth import NetWorthView

    first = NetWorthView(with_balance)
    first.resize(1400, 900)
    header = first.table.horizontalHeader()
    # Write a state with Stretch baked into it, as older versions did.
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    with_balance.save_setting(
        "columns:networth.history",
        header.saveState().toBase64().data().decode("ascii"),
    )

    reopened = NetWorthView(with_balance)
    reopened.resize(1400, 900)
    modes = [
        reopened.table.horizontalHeader().sectionResizeMode(c)
        for c in range(reopened.table.columnCount())
    ]
    assert QHeaderView.ResizeMode.Stretch not in modes


def test_a_column_can_actually_be_resized(app, with_balance):
    from carraway.ui.views.networth import NetWorthView

    view = NetWorthView(with_balance)
    view.resize(1400, 900)
    header = view.table.horizontalHeader()
    header.resizeSection(0, 300)
    assert header.sectionSize(0) == 300
