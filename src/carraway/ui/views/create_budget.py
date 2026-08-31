"""Start a budget: a name, a stretch of days, and what you may spend.

The screen is built around one observation: people arrive at a budget from
whichever end they happen to know, and forcing them in through the wrong door
is why budgeting apps go unused. Someone planning a trip knows a total.
Someone looking at their payslip knows income, savings and fixed costs.
Someone who has never budgeted knows nothing and needs to be shown what they
already spend before they can decide what to change.

So all three doors open onto the same room — a table of per-category
allowances — and every figure in it stays editable. The history is evidence,
not an instruction.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...analysis import budgets as budgets_mod
from ...core.money import Money
from ..data import Ledger
from ..widgets import Card, FlowLayout, enable_row_hover, refresh_everything

_HEADERS = ["Category", "You usually spend", "Allowance"]

# Ranges people actually budget over. "Custom" is last because it is the
# fallback, not the expectation.
_PRESETS = ["This month", "Next month", "Next 30 days", "Next 7 days", "Custom"]


def _parse(text: str) -> Money | None:
    raw = text.strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return Money.parse(raw)
    except (ValueError, TypeError):
        return None


def preset_range(name: str, today: date) -> tuple[date, date] | None:
    """The dates a preset means, or None for "Custom"."""
    if name == "This month":
        start = today.replace(day=1)
        return start, _end_of_month(start)
    if name == "Next month":
        start = _end_of_month(today.replace(day=1)) + timedelta(days=1)
        return start, _end_of_month(start)
    if name == "Next 30 days":
        return today, today + timedelta(days=29)
    if name == "Next 7 days":
        return today, today + timedelta(days=6)
    return None


def _end_of_month(day: date) -> date:
    following = day.replace(day=28) + timedelta(days=4)
    return following.replace(day=1) - timedelta(days=1)


class CreateBudgetView(QWidget):
    """The whole budget-creation flow on one screen."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self._account_boxes: dict[str, QCheckBox] = {}
        self._filling = False
        self._warning = ""
        # True while income and fixed costs still hold figures the app
        # suggested, so they can be rescaled when the window changes.
        self._backwards_is_ours = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Create a budget")
        title.setObjectName("Title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Set what you are allowed to spend over a stretch of days, then check "
            "back and see how you are going."
        )
        subtitle.setObjectName("Subtitle")
        layout.addWidget(subtitle)

        layout.addWidget(self._build_basics())
        layout.addWidget(self._build_method())

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self._hover = enable_row_hover(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignRight
            ) | Qt.AlignmentFlag.AlignVCenter
            self.table.horizontalHeaderItem(column).setTextAlignment(align)
        self.table.itemChanged.connect(self._allowance_edited)
        layout.addWidget(self.table, stretch=1)

        footer = QHBoxLayout()
        self.add_category = QComboBox()
        self.add_category.setToolTip("Budget for something you have not spent on before.")
        footer.addWidget(QLabel("Add a category"))
        footer.addWidget(self.add_category)
        add = QPushButton("Add")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_category)
        footer.addWidget(add)
        footer.addStretch(1)

        self.total_label = QLabel("")
        self.total_label.setObjectName("SectionHeading")
        footer.addWidget(self.total_label)
        layout.addLayout(footer)

        self.note = QLabel("")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.create = QPushButton("Create budget")
        self.create.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create.clicked.connect(self._create)
        buttons.addWidget(self.create)
        layout.addLayout(buttons)

        self.refresh()

    # -- the panels -------------------------------------------------------

    def _build_basics(self) -> QWidget:
        card = Card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("September")
        self.name.setMinimumWidth(180)
        # A name the app suggested keeps following the dates; one the user
        # typed never gets overwritten. Without the distinction, picking
        # "Next month" after "This month" leaves a budget called August
        # covering September.
        self._name_is_ours = True
        self.name.textEdited.connect(lambda _: setattr(self, "_name_is_ours", False))
        row.addWidget(self.name)

        row.addSpacing(12)
        row.addWidget(QLabel("From"))
        self.starts = QDateEdit()
        self.starts.setCalendarPopup(True)
        self.starts.setDisplayFormat("yyyy-MM-dd")
        self.starts.dateChanged.connect(lambda _: self._dates_edited())
        row.addWidget(self.starts)
        row.addWidget(QLabel("to"))
        self.ends = QDateEdit()
        self.ends.setCalendarPopup(True)
        self.ends.setDisplayFormat("yyyy-MM-dd")
        self.ends.dateChanged.connect(lambda _: self._dates_edited())
        row.addWidget(self.ends)

        self.preset = QComboBox()
        self.preset.addItems(_PRESETS)
        self.preset.currentTextChanged.connect(self._preset_chosen)
        row.addWidget(self.preset)
        row.addStretch(1)
        outer.addLayout(row)

        # A wrapping row rather than a menu: ten accounts in a dropdown hides
        # most of them, and this is a question the user should be able to
        # answer at a glance.
        accounts_row = QHBoxLayout()
        accounts_row.setSpacing(8)
        label = QLabel("Counts spending on")
        label.setObjectName("Muted")
        accounts_row.addWidget(label, alignment=Qt.AlignmentFlag.AlignTop)

        holder = QWidget()
        self.accounts_flow = FlowLayout(holder)
        accounts_row.addWidget(holder, stretch=1)
        outer.addLayout(accounts_row)
        return card

    def _build_method(self) -> QWidget:
        card = Card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(9)

        heading = QLabel("Where should the numbers come from?")
        heading.setObjectName("SectionHeading")
        outer.addWidget(heading)

        self.method = QButtonGroup(self)
        self.method.setExclusive(True)

        self.by_history = QRadioButton("What I usually spend")
        self.by_history.setToolTip(
            "Fills the table with your median monthly spending, scaled to this "
            "window. Not advice — this is what happens if nothing changes."
        )
        self.by_history.setChecked(True)
        self.method.addButton(self.by_history, 0)
        outer.addWidget(self.by_history)

        total_row = QHBoxLayout()
        total_row.setSpacing(8)
        self.by_total = QRadioButton("A total of")
        self.by_total.setToolTip(
            "Split across categories in proportion to what you normally spend."
        )
        self.method.addButton(self.by_total, 1)
        total_row.addWidget(self.by_total)
        self.total_input = QLineEdit()
        self.total_input.setPlaceholderText("1200.00")
        self.total_input.setMaximumWidth(120)
        total_row.addWidget(self.total_input)
        total_row.addStretch(1)
        outer.addLayout(total_row)

        back_row = QHBoxLayout()
        back_row.setSpacing(8)
        self.by_backwards = QRadioButton("Work backwards:")
        self.by_backwards.setToolTip(
            "Whatever is left after saving what you want and paying what you must "
            "is what you may spend."
        )
        self.method.addButton(self.by_backwards, 2)
        back_row.addWidget(self.by_backwards)
        self.backwards_label = QLabel("I'll make")
        back_row.addWidget(self.backwards_label)
        self.income_input = QLineEdit()
        self.income_input.setMaximumWidth(100)
        back_row.addWidget(self.income_input)
        back_row.addWidget(QLabel("· save"))
        self.saving_input = QLineEdit()
        self.saving_input.setMaximumWidth(100)
        back_row.addWidget(self.saving_input)
        back_row.addWidget(QLabel("· fixed costs"))
        self.fixed_input = QLineEdit()
        self.fixed_input.setMaximumWidth(100)
        back_row.addWidget(self.fixed_input)
        back_row.addStretch(1)
        outer.addLayout(back_row)

        self.method_note = QLabel("")
        self.method_note.setObjectName("Muted")
        self.method_note.setWordWrap(True)
        outer.addWidget(self.method_note)

        # Typing in a method's own box is a clearer statement of intent than
        # the radio button beside it, so it selects that method too.
        self.total_input.textEdited.connect(lambda _: self.by_total.setChecked(True))
        for box in (self.income_input, self.saving_input, self.fixed_input):
            box.textEdited.connect(lambda _: self.by_backwards.setChecked(True))
            box.textEdited.connect(lambda _: setattr(self, "_backwards_is_ours", False))
        self.method.idToggled.connect(lambda _i, on: self._fill() if on else None)
        for box in (self.total_input, self.income_input, self.saving_input, self.fixed_input):
            box.textChanged.connect(lambda _: self._fill())
        return card

    # -- state ------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild from the ledger: accounts, categories, and a fresh suggestion."""
        self._build_accounts()
        chosen = self.add_category.currentText()
        self.add_category.clear()
        self.add_category.addItems(list(self.ledger.categories_available))
        index = self.add_category.findText(chosen)
        if index >= 0:
            self.add_category.setCurrentIndex(index)

        if not self.starts.date().isValid() or not self.name.text():
            self._preset_chosen(self.preset.currentText())
        self._prefill_backwards()
        self._fill()

    def _build_accounts(self) -> None:
        while self.accounts_flow.count():
            item = self.accounts_flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._account_boxes = {}

        every = QCheckBox("All accounts")
        every.setChecked(True)
        every.setToolTip(
            "A card, a debit card and cash are all just ways of spending. Untick "
            "to watch only some of them."
        )
        every.toggled.connect(self._all_accounts_toggled)
        self._all_accounts = every
        self.accounts_flow.addWidget(every)

        for account in self.ledger.accounts:
            if account.closed:
                continue
            box = QCheckBox(account.name[:24])
            box.setToolTip(f"{account.name} — {account.institution or account.type}")
            box.setChecked(False)
            box.toggled.connect(lambda _on: self._account_toggled())
            self._account_boxes[account.id] = box
            self.accounts_flow.addWidget(box)

    def _all_accounts_toggled(self, on: bool) -> None:
        if on:
            self._filling = True
            for box in self._account_boxes.values():
                box.setChecked(False)
            self._filling = False
        self._fill()

    def _account_toggled(self) -> None:
        if self._filling:
            return
        if any(box.isChecked() for box in self._account_boxes.values()):
            self._filling = True
            self._all_accounts.setChecked(False)
            self._filling = False
        elif not self._all_accounts.isChecked():
            # Unticking the last one means "all" again, rather than a budget
            # that watches nothing and can never register any spending.
            self._filling = True
            self._all_accounts.setChecked(True)
            self._filling = False
        self._fill()

    def chosen_accounts(self) -> tuple[str, ...]:
        """The scope, as the Budget wants it. Empty means every account."""
        if self._all_accounts.isChecked():
            return ()
        return tuple(aid for aid, box in self._account_boxes.items() if box.isChecked())

    def _preset_chosen(self, name: str) -> None:
        window = preset_range(name, date.today())
        if window is None:
            return
        start, end = window
        self._filling = True
        self.starts.setDate(QDate(start.year, start.month, start.day))
        self.ends.setDate(QDate(end.year, end.month, end.day))
        self._filling = False
        self._retitle(start, end)
        self._prefill_backwards()
        self._fill()

    def _retitle(self, start: date, end: date) -> None:
        """Rename to match the window, unless the user named it themselves."""
        if self._name_is_ours or not self.name.text().strip():
            self.name.setText(self._default_name(start, end))
            self._name_is_ours = True

    def _default_name(self, start: date, end: date) -> str:
        """A name that reads like the window, so the sidebar is legible."""
        if start.day == 1 and end == _end_of_month(start):
            return start.strftime("%B %Y")
        return f"{start.strftime('%-d %b')}–{end.strftime('%-d %b')}"

    def _dates_edited(self) -> None:
        if self._filling:
            return
        if self.preset.currentText() != "Custom":
            self.preset.blockSignals(True)
            self.preset.setCurrentText("Custom")
            self.preset.blockSignals(False)
        self._retitle(*self.date_range())
        self._prefill_backwards()
        self._fill()

    def date_range(self) -> tuple[date, date]:
        """The chosen window. Deliberately not called `window`: that is
        QWidget's own method for the top-level window, and shadowing it breaks
        anything that walks up the widget tree."""
        return self.starts.date().toPython(), self.ends.date().toPython()

    def _prefill_backwards(self) -> None:
        """Offer what the app already knows, scaled to the window.

        These are figures *for this budget's window*, not per month: asking
        "what will you make in September" and "what will you make over these
        eleven days" are different questions, and quietly treating a monthly
        answer as an eleven-day one would inflate the budget threefold.
        """
        if not self._backwards_is_ours:
            return
        start, end = self.date_range()
        days = max((end - start).days + 1, 1)
        income = budgets_mod.scale_to_window(self.ledger.typical_monthly_income(), days)
        fixed = budgets_mod.scale_to_window(self.ledger.committed_per_month(), days)
        self._filling = True
        self.income_input.setText(f"{income.decimal:.2f}" if income.minor else "")
        self.fixed_input.setText(f"{fixed.decimal:.2f}" if fixed.minor else "")
        self._filling = False
        self.backwards_label.setText(f"Over these {days} days I'll make")

    # -- filling the table ------------------------------------------------

    def _fill(self) -> None:
        """Recompute the envelopes for whichever method is selected."""
        if self._filling:
            return
        start, end = self.date_range()
        if end < start:
            self._show([], "The end date is before the start date.")
            return
        days = (end - start).days + 1

        accounts = self.chosen_accounts() or None
        suggested = {
            line.category: line.allowance
            for line in self.ledger.suggest_envelopes(start, end, accounts)
        }

        if self.by_history.isChecked():
            lines = [budgets_mod.Envelope(c, a) for c, a in suggested.items()]
            self.method_note.setText(
                f"What {len(lines)} categories would cost over these {days} "
                "days at your usual rate."
            )
        else:
            weights = self.ledger.spending_weights(accounts)
            if self.by_total.isChecked():
                total = _parse(self.total_input.text())
                if total is None or total.minor <= 0:
                    self._show([], "Type a total to split across your categories.", suggested)
                    return
                lines = budgets_mod.split(total, weights)
                self.method_note.setText(
                    f"{total.format()} split in proportion to what you normally spend."
                )
            else:
                income = _parse(self.income_input.text())
                saving = _parse(self.saving_input.text()) or Money.zero()
                fixed = _parse(self.fixed_input.text()) or Money.zero()
                if income is None:
                    self._show([], "Say what you expect to make.", suggested)
                    return
                spendable = budgets_mod.spendable(income, saving, fixed)
                if spendable.minor <= 0:
                    self._show(
                        [],
                        f"Saving {saving.format()} and paying {fixed.format()} leaves nothing "
                        f"out of {income.format()} — {abs(spendable).format()} short. "
                        "Something has to give before this is a budget.",
                        suggested,
                    )
                    return
                # The fixed costs the user just declared are the rent and the
                # bills. Splitting the leftover across every category would
                # budget for rent a second time, so commitments take their own
                # line at their own size and only the remainder is shared out.
                committed = {
                    name: budgets_mod.scale_to_window(amount, days)
                    for name, amount in self.ledger.committed_by_category().items()
                }
                fixed_lines = budgets_mod.split(fixed, committed)
                free = {n: w for n, w in weights.items() if n not in committed}
                lines = fixed_lines + budgets_mod.split(spendable, free or weights)
                self.method_note.setText(
                    f"{income.format()} less {saving.format()} saved leaves "
                    f"{(income - saving).format()}: {fixed.format()} of it already "
                    f"committed, {spendable.format()} free to spend."
                )
        self._show(lines, "", suggested)

    def _show(self, lines, warning: str, suggested: dict | None = None) -> None:
        """Put `lines` in the table, with the usual spend beside each."""
        suggested = suggested or {}
        self._filling = True
        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            name = QTableWidgetItem(line.category)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)

            usual = suggested.get(line.category)
            typical = QTableWidgetItem(usual.format() if usual else "—")
            typical.setFlags(typical.flags() & ~Qt.ItemFlag.ItemIsEditable)
            typical.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if usual:
                typical.setToolTip(
                    "Your median monthly spend here, scaled to this window. "
                    "Evidence, not an instruction."
                )

            allowance = QTableWidgetItem(f"{line.allowance.decimal:.2f}")
            allowance.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, typical)
            self.table.setItem(row, 2, allowance)
        self._filling = False
        self._warning = warning
        self._update_total()

    def _allowance_edited(self, item: QTableWidgetItem) -> None:
        """A hand-typed allowance is the user's decision and outranks the method."""
        if self._filling or item.column() != 2:
            return
        self._update_total()

    def envelopes(self) -> list:
        out = []
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0)
            amount = _parse(self.table.item(row, 2).text()) if self.table.item(row, 2) else None
            if name is None or amount is None or amount.minor <= 0:
                continue
            out.append(budgets_mod.Envelope(name.text(), abs(amount)))
        return out

    def _provisional(self):
        """The budget as it stands, for checks that need a whole Budget.

        Given a throwaway id so it never matches a saved one, which is what
        keeps `clashes` from comparing it against itself.
        """
        start, end = self.date_range()
        if end < start:
            return None
        return budgets_mod.Budget(
            id="__draft__",
            name=self.name.text().strip() or "This budget",
            starts_on=start,
            ends_on=end,
            envelopes=tuple(self.envelopes()),
            accounts=self.chosen_accounts(),
        )

    def _check_clashes(self) -> str:
        """Whether this budget can be followed alongside the ones already saved."""
        draft = self._provisional()
        if draft is None or not draft.envelopes:
            return ""
        return budgets_mod.describe_clashes(budgets_mod.clashes(draft, self.ledger.budgets))

    def _update_total(self) -> None:
        lines = self.envelopes()
        total = Money.zero()
        for line in lines:
            total = total + line.allowance
        start, end = self.date_range()
        days = max((end - start).days + 1, 1)
        per_day = Money(total.minor // days, total.currency)
        self.total_label.setText(
            f"{total.format()} over {days} days  ·  {per_day.format()}/day"
            if lines
            else "Nothing budgeted yet"
        )
        self.create.setEnabled(bool(lines))

        # A problem with this budget outranks a clash with another one: the
        # user cannot act on "overlaps September" while the numbers in front
        # of them do not add up. Overlaps warn rather than block, because
        # "the trip is meant to blow the month" is a thing a person may decide.
        message = self._warning or self._check_clashes()
        self.note.setObjectName("Danger" if message else "Muted")
        self.note.setText(message)
        self.note.style().unpolish(self.note)
        self.note.style().polish(self.note)

    def _add_category(self) -> None:
        name = self.add_category.currentText()
        if not name:
            return
        existing = {self.table.item(r, 0).text() for r in range(self.table.rowCount())}
        if name in existing:
            self.note.setText(f"{name} is already in this budget.")
            return
        self._filling = True
        row = self.table.rowCount()
        self.table.insertRow(row)
        label = QTableWidgetItem(name)
        label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
        typical = QTableWidgetItem("—")
        typical.setFlags(typical.flags() & ~Qt.ItemFlag.ItemIsEditable)
        typical.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        amount = QTableWidgetItem("0.00")
        amount.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 0, label)
        self.table.setItem(row, 1, typical)
        self.table.setItem(row, 2, amount)
        self._filling = False
        self.note.setText(f"Added {name}. Type what you want to allow for it.")
        self.table.editItem(amount)
        self._update_total()

    # -- creating ---------------------------------------------------------

    def _create(self) -> None:
        start, end = self.date_range()
        if end < start:
            self.note.setText("The end date is before the start date.")
            return
        lines = self.envelopes()
        if not lines:
            self.note.setText("Give at least one category an allowance.")
            return

        name = self.name.text().strip() or self._default_name(start, end)
        if any(b.name == name for b in self.ledger.budgets):
            answer = QMessageBox.question(
                self,
                "Same name",
                f"You already have a budget called “{name}”. Create another?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        backwards = self.by_backwards.isChecked()
        budget = budgets_mod.Budget(
            id=uuid.uuid4().hex[:12],
            name=name,
            starts_on=start,
            ends_on=end,
            envelopes=tuple(lines),
            accounts=self.chosen_accounts(),
            # Kept only when it is the user's own reasoning, so that "why is
            # this $1,300?" stays answerable later.
            expected_income=_parse(self.income_input.text()) if backwards else None,
            savings_target=_parse(self.saving_input.text()) if backwards else None,
            fixed_costs=_parse(self.fixed_input.text()) if backwards else None,
        )
        self.ledger.save_budget(budget)
        self.name.clear()
        self._name_is_ours = True
        # Rebuilds the sidebar so the new budget appears under My budgets, and
        # every other screen alongside it.
        refresh_everything(self)

        # Jump straight to it: someone who has just made a budget wants to see
        # it, not stay on the form that made it.
        show = getattr(self.window(), "show_budget", None)
        if callable(show):
            show(budget.id)
        else:
            self.note.setText(f"Created “{name}”. It is in the sidebar under My budgets.")
