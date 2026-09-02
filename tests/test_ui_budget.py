"""The create-a-budget screen: the controls, and what they claim.

Two things are worth pinning down here. The first is the standing rule from
test_ui_views.py — every handler gets invoked at least once, because ruff
cannot see through an attribute access and a button wired to a method that
does not exist simply does nothing when clicked.

The second is that this screen makes claims. It prefills income and fixed
costs and tells the user where those figures came from, and a provenance line
that has drifted away from the number it describes is worse than no line at
all: it is confidently wrong about the one thing it exists to explain.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

pytest.importorskip("PySide6", reason="GUI tests need the [gui] extra")

from PySide6.QtCore import QDate, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from carraway.core import db  # noqa: E402
from carraway.core.models import Account, AccountType  # noqa: E402
from carraway.core.money import Money  # noqa: E402
from carraway.importers.csv_importer import import_csv  # noqa: E402
from carraway.ui.data import Ledger  # noqa: E402
from carraway.ui.views import create_budget  # noqa: E402
from carraway.ui.views.create_budget import CreateBudgetView  # noqa: E402
from carraway.ui.widgets import InfoDot  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _rows() -> str:
    """Six complete months of pay, rent and discretionary spending."""
    today = date.today()
    rows = ["Date,Description,Amount"]
    month = today.replace(day=1)
    for _ in range(7):
        month = (month - timedelta(days=1)).replace(day=1)
        rows.append(f"{month.replace(day=2)},ACME CORP PAYROLL,4000.00")
        rows.append(f"{month.replace(day=3)},GREAT LANDLORD RENT,-1200.00")
        rows.append(f"{month.replace(day=8)},CORNER BISTRO,-200.00")
        rows.append(f"{month.replace(day=9)},METRO TRANSIT,-100.00")
    return "\n".join(rows) + "\n"


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    path = tmp_path / "budget.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Checking", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="a2", name="Card", type=AccountType.CREDIT_CARD))
    txs, _ = import_csv(io.StringIO(_rows()), "a1")
    db.insert_transactions(conn, txs)
    conn.close()
    led = Ledger(path=path)
    led.load()
    return led


@pytest.fixture
def view(app, ledger) -> CreateBudgetView:
    return CreateBudgetView(ledger)


# -- the screen says where its numbers come from ------------------------


def test_the_table_says_what_the_usual_column_means(view):
    said = view.basis_note.text()
    assert "complete month" in said
    assert "Median" in said


def test_the_basis_line_names_the_month_it_leaves_out(view):
    """A budget set on the 3rd would otherwise come out at a tenth of the truth."""
    this_month = date.today().strftime("%B")
    assert this_month in view.basis_note.text()


def test_thin_history_is_admitted_rather_than_dressed_up(app, tmp_path):
    path = tmp_path / "thin.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Checking", type=AccountType.CHECKING))
    last_month = (date.today().replace(day=1) - timedelta(days=1)).replace(day=5)
    txs, _ = import_csv(
        io.StringIO(f"Date,Description,Amount\n{last_month},CORNER BISTRO,-40.00\n"), "a1"
    )
    db.insert_transactions(conn, txs)
    conn.close()
    led = Ledger(path=path)
    led.load()

    assert "little to go on" in CreateBudgetView(led).basis_note.text()


def test_each_method_explains_itself_once_it_is_chosen(view):
    view.by_history.setChecked(True)
    assert "usual rate" in view.method_note.text()

    view.total_input.setText("1200.00")
    view.by_total.setChecked(True)
    assert "proportion" in view.method_note.text()

    view.by_backwards.setChecked(True)
    said = view.method_note.text()
    # Checked for substance rather than exact phrasing: what matters is that
    # the income, the saving and the committed money are all accounted for.
    assert "in, less" in said
    assert "saved" in said
    assert "committed" in said


# -- income and fixed costs come from real history ----------------------


def test_working_backwards_is_prefilled_from_what_the_app_knows(view):
    assert create_budget._parse(view.income_input.text()) is not None
    assert create_budget._parse(view.fixed_input.text()) is not None
    # Savings is a decision, not something history can answer.
    assert view.saving_input.text() == ""


def test_the_prefilled_figures_are_scaled_to_the_window_not_monthly(view):
    """A monthly figure quietly used as an eleven-day one inflates threefold."""
    view.preset.setCurrentText("Next 30 days")
    monthly = create_budget._parse(view.income_input.text())

    view.preset.setCurrentText("Next 7 days")
    weekly = create_budget._parse(view.income_input.text())

    assert weekly is not None and monthly is not None
    assert weekly.minor < monthly.minor
    # Seven days of a thirty-day figure, give or take the rounding.
    assert abs(weekly.minor - monthly.minor * 7 // 30) < monthly.minor // 10


def test_the_income_dot_explains_the_number_actually_shown(view):
    """The provenance has to describe this figure, not a generic one."""
    view.preset.setCurrentText("Next 30 days")
    shown = create_budget._parse(view.income_input.text())
    explanation = view.income_info.explanation
    assert shown is not None
    assert shown.format() in explanation
    assert "marked as income" in explanation
    assert "scaled to 30 days" in explanation


def test_the_fixed_dot_explains_the_number_actually_shown(view):
    view.preset.setCurrentText("Next 30 days")
    shown = create_budget._parse(view.fixed_input.text())
    assert shown is not None
    assert shown.format() in view.fixed_info.explanation


def test_a_typed_figure_is_never_overwritten_by_a_new_window(view):
    """Once the user has said what they make, the app stops guessing."""
    view.income_input.textEdited.emit("5000.00")
    view.income_input.setText("5000.00")
    view.preset.setCurrentText("Next 7 days")
    assert view.income_input.text() == "5000.00"


def test_commitments_take_their_own_lines_rather_than_a_share(view):
    """Splitting the leftover across every category budgets for rent twice."""
    view.preset.setCurrentText("Next 30 days")
    view.by_backwards.setChecked(True)
    categories = {view.table.item(row, 0).text() for row in range(view.table.rowCount())}
    assert "Rent/Mortgage" in categories
    rent = next(line for line in view.envelopes() if line.category == "Rent/Mortgage")
    # At its real size, not a proportional slice of what was left over.
    assert rent.allowance > Money.parse("900.00")


# -- every control carries an explanation -------------------------------


def test_every_info_dot_has_something_to_say(view):
    dots = view.findChildren(InfoDot)
    assert len(dots) >= 8
    for dot in dots:
        assert dot.explanation.strip(), "an info dot with no explanation"
        assert dot.toolTip() == dot.explanation


def test_an_info_dot_opens_a_popup_when_clicked(app, view):
    dot = view.findChildren(InfoDot)[0]
    before = len(dot.findChildren(type(dot.parent())))
    dot.explain()
    app.processEvents()
    popups = [c for c in dot.children() if c.metaObject().className() == "QFrame"]
    assert popups, "clicking the dot produced no popup"
    assert popups[0].findChild(type(view.note)).text() == dot.explanation
    assert before >= 0  # the popup is parented to the dot, not leaked globally


# -- the account scope, which is collapsed by default -------------------


def test_the_account_list_starts_collapsed(view):
    assert view.accounts_holder.isVisible() is False
    assert "every account" in view.scope_summary.text()


def test_the_change_button_reveals_and_hides_the_accounts(view):
    view.show()
    view._toggle_scope()
    assert view.accounts_holder.isVisibleTo(view) is True
    assert view.scope_toggle.text() == "Done"
    view._toggle_scope()
    assert view.accounts_holder.isVisibleTo(view) is False
    assert view.scope_toggle.text() == "Change"


def test_the_summary_names_the_accounts_that_are_ticked(view):
    view._account_boxes["a2"].setChecked(True)
    assert "Card" in view.scope_summary.text()
    assert view.chosen_accounts() == ("a2",)


def test_unticking_the_last_account_means_all_of_them_again(view):
    """A budget watching nothing can never register any spending."""
    view._account_boxes["a2"].setChecked(True)
    view._account_boxes["a2"].setChecked(False)
    assert view.chosen_accounts() == ()
    assert "every account" in view.scope_summary.text()


# -- the rest of the flow still works -----------------------------------


def test_a_backwards_date_range_is_refused_rather_than_charted(view):
    start, _ = view.date_range()
    view.ends.setDate(QDate(start - timedelta(days=5)))
    assert "before the start date" in view.note.text()
    assert view.create.isEnabled() is False


def test_adding_a_category_puts_it_in_the_table(view):
    view.add_category.setCurrentText("Travel")
    before = view.table.rowCount()
    view._add_category()
    assert view.table.rowCount() == before + 1
    assert "Travel" in view.note.text()


def test_adding_the_same_category_twice_says_so(view):
    view.add_category.setCurrentText("Travel")
    view._add_category()
    view._add_category()
    assert "already in this budget" in view.note.text()


def test_creating_saves_the_budget_and_its_reasoning(view, ledger, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.preset.setCurrentText("Next 30 days")
    view.by_backwards.setChecked(True)
    view._create()

    ledger.load()
    assert len(ledger.budgets) == 1
    saved = ledger.budgets[0]
    assert saved.envelopes
    # The backwards reasoning is kept, so "why is this $1,300?" stays answerable.
    assert saved.expected_income is not None
    assert saved.fixed_costs is not None


def test_creating_from_history_keeps_no_backwards_reasoning(view, ledger):
    view.by_history.setChecked(True)
    view._create()
    ledger.load()
    assert ledger.budgets[0].expected_income is None


def test_a_budget_with_no_allowances_is_not_created(view, ledger):
    view.table.setRowCount(0)
    view._create()
    assert "at least one category" in view.note.text()
    ledger.load()
    assert ledger.budgets == []


def test_the_total_line_reports_the_window_and_a_daily_rate(view):
    view.preset.setCurrentText("Next 30 days")
    said = view.total_label.text()
    assert "over 30 days" in said
    assert "/day" in said


def test_the_screen_survives_a_ledger_with_nothing_in_it(app, tmp_path):
    """Someone opens this on their first day, before importing anything."""
    empty = Ledger(path=tmp_path / "empty.db")
    empty.load()
    screen = CreateBudgetView(empty)
    assert screen.table.rowCount() == 0
    assert "nothing to suggest" in screen.basis_note.text()
    assert screen.create.isEnabled() is False


def test_preset_ranges_are_what_they_say(view):
    today = date.today()
    start, end = create_budget.preset_range("This month", today)
    assert start == today.replace(day=1)
    assert end.month == today.month

    start, end = create_budget.preset_range("Next 7 days", today)
    assert (start, end) == (today, today + timedelta(days=6))

    assert create_budget.preset_range("Custom", today) is None


# -- the table's two halves, and its totals -----------------------------


def _backwards(view, saving: str = "500"):
    """This ledger makes about $3,900 with $1,180 committed, so the savings
    figures here stay well inside what it can actually absorb."""
    view.by_backwards.setChecked(True)
    view.saving_input.setText(saving)
    return view


def _bands(view) -> list[str]:
    out = []
    for row in range(view.table.rowCount()):
        item = view.table.item(row, 0)
        if item is not None and not item.data(Qt.ItemDataRole.UserRole):
            out.append(item.text())
    return out


def test_the_table_is_divided_into_what_can_and_cannot_change(view):
    _backwards(view)
    bands = _bands(view)
    assert any("can change" in band for band in bands)


def test_the_total_row_is_pinned_below_the_table(view):
    """As the last row of a scrolling table it was under the fold exactly
    when it mattered."""
    _backwards(view)
    assert view.totals_row.isVisibleTo(view)
    assert view.total_cells[0].text() == "Total"
    assert view.total_cells[2].text().startswith("$")


def test_the_total_row_is_not_mistaken_for_a_category(view):
    """It used to be read back as another envelope, counting the whole
    budget twice and offering to save a category called "Total"."""
    _backwards(view)
    names = [envelope.category for envelope in view.envelopes()]
    assert "Total" not in names
    assert not any(band in names for band in _bands(view))


def test_the_header_total_matches_the_pinned_total(view):
    _backwards(view)
    total = view.total_cells[2].text()
    assert total in view.total_label.text()


def test_typing_a_savings_target_moves_every_allowance(view):
    """The whole point of the screen: it answers as you change your mind."""
    _backwards(view, "200")
    relaxed = view.total_cells[2].text()
    _backwards(view, "1500")
    tight = view.total_cells[2].text()
    assert relaxed != tight


def test_a_savings_target_that_cannot_be_met_says_so(view):
    _backwards(view, "999999")
    assert "short" in view.note.text().lower() or "short" in view.method_note.text().lower()


def test_the_total_box_suggests_what_is_usually_spent(view):
    """Rather than a made-up figure with no relation to the user's life."""
    view.by_history.setChecked(True)
    assert "usually spend" in view.total_input.placeholderText()
