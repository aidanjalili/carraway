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

Every suggested figure says where it came from. A prefilled box is a claim,
and a claim with no source can only be accepted on faith or deleted; being
told it is the monthly rate of the two things you marked as income is what
makes it possible to disagree with. The same reasoning puts an "i" beside
each control rather than a tooltip: the person who needs the explanation is
the one who does not yet know there is a question to ask.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
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
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...analysis import budgets as budgets_mod
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import Card, FlowLayout, InfoDot, enable_row_hover, refresh_everything

_HEADERS = ["Category", "You usually spend", "Allowance", "Change"]

# Wide enough for a five-figure sum and the placeholder beside it. The boxes
# were sized to 100px, which could not show "4164.35" and its hint at once --
# the field meant to suggest a number could not display the number.
_FIGURE_WIDTH = 150

# Tall enough that the editor Qt opens inside a cell is not clipped. At the
# default height the text of an allowance being typed was cut off top and
# bottom, which is the one cell on the screen a person actually types into.
_ROW_HEIGHT = 34

# A floor for the numeric columns, wide enough for the cell editor rather than
# just for the text it displays.
_FIGURE_COLUMN = 110

# The two halves of the table. Everything above the divider is a choice;
# everything below it is a bill that has already been decided.
_FLEXIBLE_HEADING = "You can change these"
_LOCKED_HEADING = "You cannot change these this month"

# Ranges people actually budget over. "Custom" is last because it is the
# fallback, not the expectation.
_PRESETS = ["This month", "Next month", "Next 30 days", "Next 7 days", "Custom"]

# The explanations behind each "i". Kept together so they can be read as a
# set and checked for contradicting each other, which is how help text goes
# stale — one sentence gets corrected and its neighbour does not.
_HELP = {
    "window": (
        "The stretch of days this budget covers. Both ends count, so 1–30 "
        "September is thirty days.\n\n"
        "It does not have to be a month. A budget for the eleven days you are "
        "away is a perfectly good budget, and every suggested figure is scaled "
        "to whatever length you pick."
    ),
    "accounts": (
        "Which accounts count as spending against this budget.\n\n"
        "All of them, normally: a card, a debit card and cash are all just ways "
        "of spending, and dinner paid for on a credit card is not cheaper than "
        "dinner paid for in cash. Narrow it only when one account is genuinely "
        "a separate pot — a shared card, or a trip paid for out of one place."
    ),
    "history": (
        "Fills the table with what these categories normally cost you, scaled "
        "to the length of this budget.\n\n"
        "This is not advice to spend that much. It is what will happen if "
        "nothing changes, which is the number you need in front of you before "
        "you can decide what to cut."
    ),
    "total": (
        "You name one figure, and it is divided between your categories in "
        "proportion to what you normally spend on each.\n\n"
        "Proportional rather than equal on purpose: finding $50 in a $600 "
        "grocery bill and $50 in a $60 coffee habit are very different "
        "requests, and only one of them is reasonable."
    ),
    "backwards": (
        "Start from your payslip instead of your spending. Whatever is left "
        "after saving what you want to save and paying what you have to pay is "
        "what you are free to spend.\n\n"
        "Carraway fills in income and fixed costs from what it already knows; "
        "the savings target is yours to set, since it is a decision rather "
        "than a fact."
    ),
    "income": (
        "What you expect to receive over this budget's window — not per month, "
        "unless the window happens to be a month.\n\n"
        "The suggested figure counts only recurring income, because that is "
        "the part you can rely on arriving again. A one-off deposit is real "
        "money, but budgeting against it plans to receive it twice."
    ),
    "saving": (
        "What you want to put aside over this window, taken off the top before "
        "anything is allocated to spending.\n\n"
        "Left empty because it is a decision, not something the app can read "
        "off your history. Saving what happens to be left over is how people "
        "end up saving nothing."
    ),
    "fixed": (
        "The part already spoken for: rent, bills, subscriptions.\n\n"
        "These get their own lines in the table at their real size rather than "
        "a share of the leftover, so rent is not budgeted for twice. Habits are "
        "deliberately not counted here — those are spending, and the point of "
        "the question is to keep the two apart."
    ),
    "usual": (
        "The median of what you spent on this category in each complete "
        "calendar month, converted to a daily rate and scaled to this budget's "
        "window.\n\n"
        "The median rather than the average, so one December or one wedding "
        "does not raise the figure permanently. The month in progress is left "
        "out entirely: it is always short, and including it would make a "
        "budget set on the 3rd come out at a tenth of the truth."
    ),
    "allowance": (
        "What you are allowing yourself. Type over any of these — a figure you "
        "chose beats one the app worked out, and nothing here is recalculated "
        "behind you once you have edited it."
    ),
}


def _let_it_wrap(label: QLabel) -> None:
    """Make a layout give a wrapped label the height it actually needs.

    A word-wrapped QLabel reports the width of one long line and a single
    line's height, so a two-line explanation gets its second line clipped by
    whatever card it sits in. Turning on height-for-width is what makes the
    layout ask the label how tall it wants to be at the width it was given.
    """
    label.setWordWrap(True)
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    policy.setVerticalPolicy(QSizePolicy.Policy.MinimumExpanding)
    label.setSizePolicy(policy)
    # Height-for-width alone still leaves the last line grazing the card edge,
    # because the card is sized before the label has been given its width.
    # Two lines' worth of room is what these notes actually run to.
    label.setMinimumHeight(label.fontMetrics().height() * 2 + 2)


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
        self._has_totals = False
        self._warning = ""
        # A note about something the user just did, shown when nothing is
        # actually wrong. Rendered by _update_total like everything else.
        self._hint = ""
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
        layout.addWidget(self._build_table(), stretch=1)

        buttons = QHBoxLayout()
        self.note = QLabel("")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        buttons.addWidget(self.note, stretch=1)
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
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("September")
        self.name.setMinimumWidth(160)
        # A name the app suggested keeps following the dates; one the user
        # typed never gets overwritten. Without the distinction, picking
        # "Next month" after "This month" leaves a budget called August
        # covering September.
        self._name_is_ours = True
        self.name.textEdited.connect(lambda _: setattr(self, "_name_is_ours", False))
        row.addWidget(self.name)

        row.addSpacing(10)
        self.preset = QComboBox()
        self.preset.addItems(_PRESETS)
        self.preset.currentTextChanged.connect(self._preset_chosen)
        row.addWidget(self.preset)

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
        row.addWidget(InfoDot(_HELP["window"]))
        row.addStretch(1)
        outer.addLayout(row)

        # Collapsed by default. Every account is the answer nearly every time,
        # and eleven checkboxes wrapped over three lines made the least-changed
        # setting on the screen the loudest thing on it.
        scope_row = QHBoxLayout()
        scope_row.setSpacing(6)
        self.scope_summary = QLabel("")
        self.scope_summary.setObjectName("Muted")
        scope_row.addWidget(self.scope_summary)
        scope_row.addWidget(InfoDot(_HELP["accounts"]))
        self.scope_toggle = QPushButton("Change")
        self.scope_toggle.setFlat(True)
        self.scope_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scope_toggle.setObjectName("Accent")
        self.scope_toggle.clicked.connect(self._toggle_scope)
        scope_row.addWidget(self.scope_toggle)
        scope_row.addStretch(1)
        outer.addLayout(scope_row)

        self.accounts_holder = QWidget()
        self.accounts_flow = FlowLayout(self.accounts_holder)
        self.accounts_holder.setVisible(False)
        outer.addWidget(self.accounts_holder)
        return card

    def _build_method(self) -> QWidget:
        card = Card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(8)

        heading = QLabel("Where should the numbers come from?")
        heading.setObjectName("SectionHeading")
        outer.addWidget(heading)

        self.method = QButtonGroup(self)
        self.method.setExclusive(True)

        history_row = QHBoxLayout()
        history_row.setSpacing(6)
        self.by_history = QRadioButton("What I usually spend")
        self.by_history.setChecked(True)
        self.method.addButton(self.by_history, 0)
        history_row.addWidget(self.by_history)
        history_row.addWidget(InfoDot(_HELP["history"]))
        history_row.addStretch(1)
        outer.addLayout(history_row)

        total_row = QHBoxLayout()
        total_row.setSpacing(6)
        self.by_total = QRadioButton("A total of")
        self.method.addButton(self.by_total, 1)
        total_row.addWidget(self.by_total)
        self.total_input = QLineEdit()
        # Filled in from real spending by `_fill`; this is only what shows
        # before there is any history to draw on.
        self.total_input.setPlaceholderText("1200.00")
        self.total_input.setMinimumWidth(_FIGURE_WIDTH)
        total_row.addWidget(self.total_input)
        self.total_info = InfoDot(_HELP["total"])
        total_row.addWidget(self.total_info)
        total_row.addStretch(1)
        outer.addLayout(total_row)

        back_row = QHBoxLayout()
        back_row.setSpacing(6)
        self.by_backwards = QRadioButton("Work backwards")
        self.method.addButton(self.by_backwards, 2)
        back_row.addWidget(self.by_backwards)
        back_row.addWidget(InfoDot(_HELP["backwards"]))
        back_row.addStretch(1)
        outer.addLayout(back_row)

        # Indented under its radio button, so the three inputs read as
        # belonging to that method rather than to the card.
        inputs_row = QHBoxLayout()
        inputs_row.setSpacing(6)
        inputs_row.addSpacing(24)
        self.backwards_label = QLabel("I'll make")
        self.backwards_label.setObjectName("Muted")
        inputs_row.addWidget(self.backwards_label)
        self.income_input = QLineEdit()
        self.income_input.setMinimumWidth(_FIGURE_WIDTH)
        inputs_row.addWidget(self.income_input)
        self.income_info = InfoDot(_HELP["income"])
        inputs_row.addWidget(self.income_info)

        save_label = QLabel("save")
        save_label.setObjectName("Muted")
        inputs_row.addWidget(save_label)
        self.saving_input = QLineEdit()
        self.saving_input.setMinimumWidth(_FIGURE_WIDTH)
        self.saving_input.setPlaceholderText("0.00")
        inputs_row.addWidget(self.saving_input)
        inputs_row.addWidget(InfoDot(_HELP["saving"]))

        fixed_label = QLabel("fixed costs")
        fixed_label.setObjectName("Muted")
        inputs_row.addWidget(fixed_label)
        self.fixed_input = QLineEdit()
        self.fixed_input.setMinimumWidth(_FIGURE_WIDTH)
        inputs_row.addWidget(self.fixed_input)
        self.fixed_info = InfoDot(_HELP["fixed"])
        inputs_row.addWidget(self.fixed_info)
        inputs_row.addStretch(1)
        outer.addLayout(inputs_row)

        # One line under the whole card, describing whichever method is live.
        # Three permanent explanations would be three-quarters noise.
        self.method_note = QLabel("")
        self.method_note.setObjectName("Muted")
        self.method_note.setWordWrap(True)
        _let_it_wrap(self.method_note)
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

    def _build_table(self) -> QWidget:
        card = Card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(8)

        head_row = QHBoxLayout()
        head_row.setSpacing(6)
        heading = QLabel("What you may spend")
        heading.setObjectName("SectionHeading")
        head_row.addWidget(heading)
        head_row.addWidget(InfoDot(_HELP["allowance"]))
        head_row.addStretch(1)
        self.total_label = QLabel("")
        self.total_label.setObjectName("SectionHeading")
        head_row.addWidget(self.total_label)
        outer.addLayout(head_row)

        # Where the middle column's numbers come from. Said once, above the
        # table, rather than hidden in a tooltip on every cell.
        self.basis_note = QLabel("")
        self.basis_note.setObjectName("Muted")
        _let_it_wrap(self.basis_note)
        outer.addWidget(self.basis_note)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        # ResizeToContents measures the text, not the editor Qt opens on top
        # of it -- which carries its own border and padding, so a column
        # sized to "$162.31" clipped the box you type into and showed the
        # figure as a row of dots. A floor under every numeric column.
        self.table.horizontalHeader().setMinimumSectionSize(_FIGURE_COLUMN)
        self.table.setAlternatingRowColors(True)
        self._hover = enable_row_hover(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignRight
            ) | Qt.AlignmentFlag.AlignVCenter
            self.table.horizontalHeaderItem(column).setTextAlignment(align)
        self.table.horizontalHeaderItem(1).setToolTip(_HELP["usual"])
        self.table.itemChanged.connect(self._allowance_edited)
        outer.addWidget(self.table, stretch=1)

        # The total sits below the table rather than in it. As the last row of
        # a scrolling table it was under the fold exactly when it mattered --
        # a dozen categories is enough to push it out of sight, and the total
        # is the line that says whether the plan adds up.
        self.totals_row = QWidget()
        totals_layout = QHBoxLayout(self.totals_row)
        totals_layout.setContentsMargins(4, 6, 0, 0)
        totals_layout.setSpacing(0)
        self.total_cells: list[QLabel] = []
        for index, text in enumerate(("Total", "", "", "")):
            cell = QLabel(text)
            font = cell.font()
            font.setBold(True)
            cell.setFont(font)
            cell.setAlignment(
                (Qt.AlignmentFlag.AlignLeft if index == 0 else Qt.AlignmentFlag.AlignRight)
                | Qt.AlignmentFlag.AlignVCenter
            )
            self.total_cells.append(cell)
            totals_layout.addWidget(cell, stretch=1 if index == 0 else 0)
        outer.addWidget(self.totals_row)

        # Column widths are the table's, so the footer reads as its last row
        # rather than as a separate thing that happens to sit underneath.
        self.table.horizontalHeader().sectionResized.connect(lambda *_: self._align_totals())

        footer = QHBoxLayout()
        footer.setSpacing(6)
        footer.addWidget(QLabel("Add a category"))
        self.add_category = QComboBox()
        footer.addWidget(self.add_category)
        add = QPushButton("Add")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_category)
        footer.addWidget(add)
        footer.addWidget(
            InfoDot(
                "Budget for something you have not spent on before, or have not "
                "spent on lately.\n\nA category with no history has no suggested "
                "figure, so you type the allowance yourself."
            )
        )
        footer.addStretch(1)
        outer.addLayout(footer)
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
        self._describe_scope()

    def _toggle_scope(self) -> None:
        """Show or hide the account checkboxes."""
        showing = not self.accounts_holder.isVisible()
        self.accounts_holder.setVisible(showing)
        self.scope_toggle.setText("Done" if showing else "Change")

    def _describe_scope(self) -> None:
        """Say which accounts count, in a line rather than eleven checkboxes."""
        chosen = self.chosen_accounts()
        if not chosen:
            self.scope_summary.setText("Counts spending on every account")
            return
        names = [a.name for a in self.ledger.accounts if a.id in chosen]
        if len(names) <= 2:
            self.scope_summary.setText(f"Counts spending on {' and '.join(names)}")
            return
        self.scope_summary.setText(
            f"Counts spending on {names[0]} and {len(names) - 1} other accounts"
        )

    def _all_accounts_toggled(self, on: bool) -> None:
        if on:
            self._filling = True
            for box in self._account_boxes.values():
                box.setChecked(False)
            self._filling = False
        self._describe_scope()
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
        self._describe_scope()
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
        self._income = self.ledger.income_estimate()
        self._fixed = self.ledger.fixed_costs_estimate()
        income = budgets_mod.scale_to_window(self._income.amount, days)
        fixed = budgets_mod.scale_to_window(self._fixed.amount, days)
        self._filling = True
        self.income_input.setText(f"{income.decimal:.2f}" if income.minor else "")
        self.fixed_input.setText(f"{fixed.decimal:.2f}" if fixed.minor else "")
        self._filling = False
        self.backwards_label.setText(f"Over these {days} days I'll make")

        # The "i" beside each box carries the general explanation plus where
        # this particular figure came from, since that is what someone
        # staring at a prefilled number actually wants to know.
        self.income_info.setExplanation(
            f"{_HELP['income']}\n\nThis {income.format()} is "
            f"{self._income.amount.format()} a month scaled to {days} days. "
            f"{self._income.source}"
            if income.minor
            else f"{_HELP['income']}\n\n{self._income.source}"
        )
        self.fixed_info.setExplanation(
            f"{_HELP['fixed']}\n\nThis {fixed.format()} is "
            f"{self._fixed.amount.format()} a month scaled to {days} days. "
            f"{self._fixed.source}"
            if fixed.minor
            else f"{_HELP['fixed']}\n\n{self._fixed.source}"
        )

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
        basis = self.ledger.history_basis(accounts)
        self.basis_note.setText(basis.describe())
        suggested = {
            line.category: line.allowance
            for line in self.ledger.suggest_envelopes(start, end, accounts)
        }

        # What the whole lot costs at the usual rate. Shown as the total box's
        # placeholder so "a total of…" starts from a real number the user can
        # nudge, rather than from a figure with no relationship to their life.
        usual_total = Money(sum(a.minor for a in suggested.values()))
        if usual_total.minor > 0:
            # Just the figure. Spelling out where it came from inside the box
            # made a placeholder too long to read in the box it was in, which
            # is a worse way to fail than saying nothing.
            self.total_input.setPlaceholderText(f"{usual_total.decimal:.2f}")
            self.total_info.set_explanation(
                f"{_HELP['total']}\n\nThe box starts from {usual_total.format()}, "
                "which is what these categories usually cost you over a window "
                "this long. Type over it with whatever you would rather spend."
            )

        if self.by_history.isChecked():
            lines = [budgets_mod.Envelope(c, a) for c, a in suggested.items()]
            self.method_note.setText(
                f"What {len(lines)} categories would cost over these {days} days at "
                "your usual rate — not a recommendation, just what happens if "
                "nothing changes."
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
                    f"{total.format()} split across {len(lines)} categories in "
                    "proportion to what you normally spend on each."
                )
            else:
                income = _parse(self.income_input.text())
                saving = _parse(self.saving_input.text()) or Money.zero()
                if income is None:
                    self._show([], "Say what you expect to make.", suggested)
                    return

                budgeted = income - saving
                committed = {
                    name: budgets_mod.scale_to_window(amount, days)
                    for name, amount in self.ledger.committed_by_category().items()
                }
                fixed = Money(sum(a.minor for a in committed.values()))
                # Kept in step with the estimate, because it is derived from
                # the same commitments and a stale figure beside a live one
                # invites the reader to trust the wrong one.
                if not self.fixed_input.hasFocus():
                    self.fixed_input.setText(f"{fixed.decimal:.2f}")

                if budgeted.minor <= fixed.minor:
                    short = Money(fixed.minor - budgeted.minor + 1)
                    self._show(
                        [],
                        f"Saving {saving.format()} out of {income.format()} leaves "
                        f"{budgeted.format()}, and {fixed.format()} of that is already "
                        f"committed — {short.format()} short before you spend anything. "
                        "The savings target has to come down, or the commitments do.",
                        suggested,
                    )
                    return

                # `suggested`, not `weights`: the weights are monthly medians
                # and the budget is for this window. Mixing the two showed a
                # $948 monthly rent against a 30-day budget that had allowed
                # $934 for it, and on a one-week window it would be out by a
                # factor of four.
                lines = budgets_mod.plan(budgeted, suggested, committed)
                spare = Money(budgeted.minor - fixed.minor)
                needed = Money(sum(line.change.minor for line in lines if not line.locked))
                if needed.minor < 0:
                    verdict = (
                        f" To save {saving.format()} you need to find "
                        f"{abs(needed).format()} across the categories above the line."
                    )
                else:
                    verdict = " You are already inside that, with room to spare."
                self.method_note.setText(
                    f"{income.format()} in, less {saving.format()} saved, leaves "
                    f"{budgeted.format()}. {fixed.format()} of that is committed and "
                    f"cannot move this month, so {spare.format()} is shared across "
                    f"what you choose to spend.{verdict}"
                )
        self._show(lines, "", suggested)

    def _show(self, lines, warning: str, suggested: dict | None = None) -> None:
        """Put `lines` in the table, split into what can and cannot change.

        `lines` may be plain Envelopes or the richer Lines that carry a
        commitment; the ones that do get a divider, a change column and a
        total, and the ones that do not are shown the simple way.
        """
        suggested = suggested or {}
        self._filling = True
        self.table.clearSpans()

        rich = [line for line in lines if hasattr(line, "committed")]
        if rich:
            self._show_split(rich)
        else:
            self._show_flat(lines, suggested)

        self._filling = False
        self._warning = warning
        # A hint is about a row that has just been added, so rebuilding the
        # table retires it.
        self._hint = ""
        self._update_total()

    def _show_flat(self, lines, suggested: dict) -> None:
        """One row per envelope, with no commitment to separate out."""
        self._has_totals = False
        self.totals_row.setVisible(False)
        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            usual = suggested.get(line.category)
            self._write_row(row, line.category, usual, line.allowance, change=None)

    def _show_split(self, lines) -> None:
        """Flexible lines, a divider, locked lines, then a total.

        The divider is the point of the screen. "Spend $262 less on dining" is
        an instruction; "spend less on rent" is not, and mixing the two into
        one list leaves the reader to work out which is which.
        """
        flexible = [line for line in lines if not line.locked]
        locked = [line for line in lines if line.locked]

        rows = len(flexible) + len(locked)
        rows += 1 if flexible else 0
        rows += 1 if locked else 0
        self.table.setRowCount(rows)

        row = 0
        if flexible:
            self._write_heading(row, _FLEXIBLE_HEADING, budgets_mod.totals(flexible))
            row += 1
            for line in flexible:
                self._write_row(
                    row,
                    line.category,
                    line.usual,
                    line.allowance,
                    change=line.change,
                    committed=line.committed,
                )
                row += 1
        if locked:
            self._write_heading(row, _LOCKED_HEADING, budgets_mod.totals(locked))
            row += 1
            for line in locked:
                self._write_row(
                    row,
                    line.category,
                    line.usual,
                    line.allowance,
                    change=None,
                    committed=line.committed,
                    locked=True,
                )
                row += 1

        self._show_totals(budgets_mod.totals(lines))

    def _show_totals(self, summary) -> None:
        """The pinned footer under the table: what it all comes to."""
        self.total_cells[1].setText(summary.usual.format())
        self.total_cells[2].setText(summary.allowance.format())
        change = summary.change
        self.total_cells[3].setText(change.format() if change.minor else "no change")
        self.total_cells[3].setObjectName(
            "Danger" if change.minor < 0 else "Accent" if change.minor > 0 else "Muted"
        )
        self.total_cells[3].style().unpolish(self.total_cells[3])
        self.total_cells[3].style().polish(self.total_cells[3])
        self._has_totals = True
        self.totals_row.setVisible(True)
        self._align_totals()

    def _align_totals(self) -> None:
        head = self.table.horizontalHeader()
        for index in range(1, len(self.total_cells)):
            self.total_cells[index].setFixedWidth(head.sectionSize(index))

    def _write_heading(self, row: int, text: str, summary=None) -> None:
        """A band across the whole table, dividing the two halves.

        Carries its own subtotal. Without one, the note above the table asks
        the user to find $810 across the flexible categories while the total
        underneath says $806 -- both true, since a locked line can drift a few
        dollars from its usual, but two unexplained figures for what looks
        like one question. The subtotal is where they reconcile.
        """
        label = text
        if summary is not None:
            label = f"{text}  —  {summary.allowance.format()} of {summary.usual.format()}"
            if summary.change.minor:
                label += f", {summary.change.format()}"
        item = QTableWidgetItem(label)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setData(Qt.ItemDataRole.UserRole, False)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor(theme.ACTIVE.muted))
        self.table.setItem(row, 0, item)
        self.table.setSpan(row, 0, 1, len(_HEADERS))

    def _write_row(
        self,
        row: int,
        category: str,
        usual,
        allowance,
        *,
        change=None,
        committed=None,
        locked: bool = False,
        editable: bool = True,
        bold: bool = False,
    ) -> None:
        name = QTableWidgetItem(category)
        name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
        # Flags this as a real category rather than a heading or the total.
        # Without it `envelopes()` read the Total row as another category and
        # counted the whole budget twice -- the header said $7,650.85 for a
        # $3,358.24 budget, and "Total" would have been saved as a line.
        name.setData(Qt.ItemDataRole.UserRole, bool(editable))

        typical = QTableWidgetItem(usual.format() if usual else "—")
        typical.setFlags(typical.flags() & ~Qt.ItemFlag.ItemIsEditable)
        typical.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        typical.setToolTip(
            _HELP["usual"]
            if usual
            else "Nothing spent here in the months this is drawn from, so there "
            "is no usual figure to compare against."
        )

        allowance_item = QTableWidgetItem(allowance.format())
        allowance_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if not editable:
            allowance_item.setFlags(allowance_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if committed is not None and committed.minor > 0 and not locked:
            allowance_item.setToolTip(
                f"Includes {committed.format()} already committed, which is not "
                "yours to reduce this month."
            )

        if locked:
            text = "locked"
        elif change is None:
            text = "—"
        else:
            text = f"{change.format()}" if change.minor else "no change"
        change_item = QTableWidgetItem(text)
        change_item.setFlags(change_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        change_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if change is not None and change.minor < 0:
            # Spending less than you do now is the thing being asked for, so
            # it is marked as a demand rather than as a failure.
            change_item.setForeground(QColor(theme.ACTIVE.danger))
        elif change is not None and change.minor > 0:
            change_item.setForeground(QColor(theme.ACTIVE.accent))
        if locked:
            change_item.setToolTip("Already committed in full, so there is nothing here to change.")

        if bold:
            for item in (name, typical, allowance_item, change_item):
                font = item.font()
                font.setBold(True)
                item.setFont(font)

        self.table.setItem(row, 0, name)
        self.table.setItem(row, 1, typical)
        self.table.setItem(row, 2, allowance_item)
        self.table.setItem(row, 3, change_item)

    def _allowance_edited(self, item: QTableWidgetItem) -> None:
        """A hand-typed allowance is the user's decision and outranks the method.

        The rest of the row has to follow it. Editing one allowance without
        recomputing its change and the totals left three numbers on screen
        that no longer agreed with each other, and the whole point of the
        change column is that it answers immediately.
        """
        if self._filling or item.column() != 2:
            return
        self._recalculate()
        self._update_total()

    def _recalculate(self) -> None:
        """Refresh the change column and the totals from what is in the table.

        Reads the table rather than the plan, because by this point the user
        may have overridden any number of allowances by hand and their
        figures are the ones that count.
        """
        self._filling = True
        try:
            usual_total = allowance_total = 0
            currency = "USD"
            for row in range(self.table.rowCount()):
                name = self.table.item(row, 0)
                if name is None or not name.data(Qt.ItemDataRole.UserRole):
                    continue
                usual = _parse(self.table.item(row, 1).text()) if self.table.item(row, 1) else None
                cell = self.table.item(row, 2)
                allowance = _parse(cell.text()) if cell else None
                if allowance is None:
                    continue
                currency = allowance.currency
                allowance_total += allowance.minor
                if usual is None:
                    continue
                usual_total += usual.minor

                change_item = self.table.item(row, 3)
                if change_item is None or change_item.text() == "locked":
                    continue
                gap = Money(allowance.minor - usual.minor, allowance.currency)
                change_item.setText(gap.format() if gap.minor else "no change")
                change_item.setForeground(
                    QColor(theme.ACTIVE.danger if gap.minor < 0 else theme.ACTIVE.accent)
                    if gap.minor
                    else QColor(theme.ACTIVE.muted)
                )
            # A tracked flag, not the widget's visibility. A view that has not
            # been shown yet reports every child as invisible, so asking Qt
            # meant the totals silently stopped updating -- the same trap the
            # cash dialog hit with a checkbox earlier.
            if self._has_totals:
                self._show_totals(
                    budgets_mod.Line(
                        "Total",
                        Money(usual_total, currency),
                        Money(0, currency),
                        Money(allowance_total, currency),
                    )
                )
        finally:
            self._filling = False

    def envelopes(self) -> list:
        """The category lines, skipping the headings and the total row."""
        out = []
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0)
            if name is None or not name.data(Qt.ItemDataRole.UserRole):
                continue
            cell = self.table.item(row, 2)
            amount = _parse(cell.text()) if cell else None
            if amount is None or amount.minor <= 0:
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

    def _describe_impact(self) -> str:
        """What this budget would cost the ones it sits inside.

        Computed on the draft, so it answers before anything is saved --
        which is the whole point. "Can I afford three days away" is not
        answered by "this contradicts your month"; it is answered by what
        the other twenty-seven days are left with.
        """
        draft = self._provisional()
        if draft is None or not draft.envelopes:
            return ""
        found = budgets_mod.impacts(draft, self.ledger.budgets)
        return found[0].describe() if found else ""

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
        # A hint comes last and is not an error, so it is not painted as one.
        #
        # Everything that has something to say routes through here rather than
        # writing to the label itself: "Added Travel" used to be set and then
        # wiped by this method a line later, so the one message the user had
        # just asked for was the one that never appeared.
        # A problem outranks the impact, and the impact outranks a hint: a
        # budget that does not add up cannot be reasoned about, but one that
        # merely eats into another is a decision the user is entitled to make
        # with the arithmetic in front of them.
        problem = self._warning or self._check_clashes()
        message = problem or self._describe_impact() or self._hint
        self.note.setObjectName("Danger" if problem else "Muted")
        self.note.setText(message)
        self.note.style().unpolish(self.note)
        self.note.style().polish(self.note)

    def _add_category(self) -> None:
        name = self.add_category.currentText()
        if not name:
            return
        existing = {self.table.item(r, 0).text() for r in range(self.table.rowCount())}
        if name in existing:
            self._hint = f"{name} is already in this budget."
            self._update_total()
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
        self._hint = f"Added {name}. Type what you want to allow for it."
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

        # Tell the phone straight away. The snapshot was only published after
        # a bank sync, which is rate limited and may not happen for hours --
        # so the one moment the phone's copy is guaranteed stale, making or
        # changing a budget, was the one moment nothing refreshed it. Quiet on
        # failure: the phone shows how old its copy is, and someone who has
        # just made a budget should not be handed a network error.
        from .pocket import publish_in_background

        publish_in_background(self, self.ledger)

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
