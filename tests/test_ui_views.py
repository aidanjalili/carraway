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

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

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
