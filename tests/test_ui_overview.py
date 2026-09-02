"""The overview screen: the period picker, and not lying about an empty one.

The empty case is the one worth pinning down. On the first of the month, with
nothing synced yet, the arithmetic happily reports "net up 100%", "spending
down 100%" and five categories "stopped" -- all true, all read as good news
about a month that has not happened.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytest.importorskip("PySide6", reason="GUI tests need the [gui] extra")

from PySide6.QtWidgets import QApplication  # noqa: E402

from carraway.core import db  # noqa: E402
from carraway.core.models import Account, AccountType, Transaction  # noqa: E402
from carraway.core.money import Money  # noqa: E402
from carraway.ui.data import Ledger  # noqa: E402
from carraway.ui.views.dashboard import CUSTOM, DashboardView  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _ledger(tmp_path, when: list[date]) -> Ledger:
    path = tmp_path / "overview.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id=f"t{index}",
                account_id="a1",
                date=day,
                amount=Money.parse("-20.00"),
                description="COFFEE",
                merchant="COFFEE",
                category="Dining",
            )
            for index, day in enumerate(when)
        ],
    )
    conn.close()
    led = Ledger(path)
    led.load()
    return led


def test_the_period_choice_is_remembered(app, tmp_path):
    ledger = _ledger(tmp_path, [date.today() - timedelta(days=3)])
    view = DashboardView(ledger)
    view.preset.setCurrentText("Last 90 days")

    ledger.load()
    assert ledger.setting("overview_period") == "Last 90 days"
    assert DashboardView(ledger).preset.currentText() == "Last 90 days"


def test_the_custom_dates_only_show_for_a_custom_range(app, tmp_path):
    ledger = _ledger(tmp_path, [date.today()])
    view = DashboardView(ledger)
    view.preset.setCurrentText("Last month")
    assert view.from_date.isVisible() is False
    view.preset.setCurrentText(CUSTOM)
    assert view.from_date.isVisibleTo(view) is True


def test_a_custom_range_is_saved_and_restored(app, tmp_path):
    from PySide6.QtCore import QDate

    ledger = _ledger(tmp_path, [date(2026, 5, 10)])
    view = DashboardView(ledger)
    view.preset.setCurrentText(CUSTOM)
    view.from_date.setDate(QDate(date(2026, 5, 1)))
    view.to_date.setDate(QDate(date(2026, 5, 31)))

    ledger.load()
    assert ledger.setting("overview_custom_range") == ["2026-05-01", "2026-05-31"]

    again = DashboardView(ledger)
    assert again.from_date.date().toPython() == date(2026, 5, 1)
    period, _ = again._period()
    assert (period.starts_on, period.ends_on) == (date(2026, 5, 1), date(2026, 5, 31))


def test_a_backwards_custom_range_is_read_the_right_way_round(app, tmp_path):
    """Typing the end date first is a slip, not a request for no data."""
    from PySide6.QtCore import QDate

    ledger = _ledger(tmp_path, [date(2026, 5, 10)])
    view = DashboardView(ledger)
    view.preset.setCurrentText(CUSTOM)
    view.from_date.setDate(QDate(date(2026, 5, 31)))
    view.to_date.setDate(QDate(date(2026, 5, 1)))

    period, _ = view._period()
    assert period.starts_on == date(2026, 5, 1)
    assert period.ends_on == date(2026, 5, 31)


def test_an_empty_period_makes_no_claims_about_improvement(app, tmp_path):
    """The bug this test exists for: five categories reported as "stopped"."""
    last_month = date.today().replace(day=1) - timedelta(days=5)
    ledger = _ledger(tmp_path, [last_month])

    view = DashboardView(ledger)
    view.preset.setCurrentText("This month")

    said = view.changes_layout.itemAt(1).widget().text()
    assert "Nothing recorded in this period yet" in said
    assert "stopped" not in said
    # And no card claims anything moved.
    for card in (view.in_card, view.out_card, view.net_card, view.burn_card):
        assert getattr(card, "_comparison", None) is None or card._comparison.text() == ""


def test_all_time_says_there_is_nothing_to_compare_against(app, tmp_path):
    ledger = _ledger(tmp_path, [date.today() - timedelta(days=2)])
    view = DashboardView(ledger)
    view.preset.setCurrentText("All time")
    said = view.changes_layout.itemAt(1).widget().text()
    assert "All time" in said


def test_a_period_with_data_reports_what_moved(app, tmp_path):
    today = date.today()
    this_month = today.replace(day=1)
    previous = this_month - timedelta(days=1)
    ledger = _ledger(tmp_path, [previous, this_month, this_month])

    view = DashboardView(ledger)
    view.preset.setCurrentText("Last month")
    # A heading plus at least one row of change, not the empty note.
    assert view.changes_layout.count() >= 2
    assert "transactions" in view.range_label.text()


def test_no_transactions_at_all_says_so_rather_than_showing_zeroes(app, tmp_path):
    ledger = _ledger(tmp_path, [])
    view = DashboardView(ledger)
    assert "No transactions imported yet" in view.range_label.text()
