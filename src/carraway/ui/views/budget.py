"""Set a savings goal and see what it costs you per category.

The direction is deliberately backwards from most budgeting apps. Rather than
asking someone to invent a number for each category, they state the outcome
they want — save this much by then — and the plan works out what has to give.

Committed spending is separated from discretionary before anything is cut,
because a budget that tells you to spend less on rent is not advice.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...analysis import budget as budget_mod
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import Card, SortableItem, StatCard, StatRow

_HEADERS = ["Category", "Spending now", "Allowed", "Change"]


class BudgetView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self.plan = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("Budget")
        title.setObjectName("Title")
        subtitle = QLabel("Say what you want to save. This works out what you can spend.")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = Card()
        row = QHBoxLayout(controls)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(10)

        row.addWidget(QLabel("Save"))
        self.target = QLineEdit("5000")
        self.target.setFixedWidth(110)
        self.target.returnPressed.connect(self.recalculate)
        row.addWidget(self.target)

        row.addWidget(QLabel("within"))
        self.months = QSpinBox()
        self.months.setRange(1, 120)
        self.months.setValue(6)
        self.months.setSuffix(" months")
        row.addWidget(self.months)

        row.addWidget(QLabel("budgeting"))
        self.period = QComboBox()
        self.period.addItems(["monthly", "weekly"])
        row.addWidget(self.period)

        calculate = QPushButton("Work it out")
        calculate.setCursor(Qt.CursorShape.PointingHandCursor)
        calculate.clicked.connect(self.recalculate)
        row.addWidget(calculate)
        row.addStretch(1)
        layout.addWidget(controls)

        self.verdict_card = StatCard("Verdict", "-")
        self.needed_card = StatCard("Save per period", "-")
        self.allowed_card = StatCard("Spend per period", "-")
        self.cut_card = StatCard("Cut required", "-")
        layout.addWidget(
            StatRow([self.verdict_card, self.needed_card, self.allowed_card, self.cut_card])
        )

        self.explanation = QLabel("")
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Muted")
        layout.addWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.recalculate()

    def recalculate(self) -> None:
        raw = self.target.text().strip().replace(",", "").replace("$", "") or "0"
        try:
            target = Money.parse(raw)
        except (ValueError, TypeError):
            self.explanation.setText(f"'{self.target.text()}' is not an amount I can read.")
            return

        months = self.months.value()
        goal = budget_mod.Goal(
            target=target,
            horizon=date.today() + timedelta(days=30 * months),
            period=self.period.currentText(),
        )
        self.plan = self.ledger.budget_plan(goal, self.period.currentText())
        self.refresh()

    def refresh(self) -> None:
        plan = self.plan
        if plan is None:
            return

        self.verdict_card.set_value("Reachable" if plan.feasible else "Not reachable")
        self.verdict_card.value_label.setStyleSheet(
            f"font-size: 22px; font-weight: 600; color: "
            f"{theme.ACTIVE.accent if plan.feasible else theme.ACTIVE.danger};"
        )
        self.needed_card.set_value(plan.required.format())
        allowed = sum((c.allowance for c in plan.categories), Money.zero())
        self.allowed_card.set_value(allowed.format())
        cut = sum(
            (c.baseline - c.allowance for c in plan.categories if c.allowance < c.baseline),
            Money.zero(),
        )
        self.cut_card.set_value(cut.format() if cut else "none")

        self.explanation.setText(plan.explanation)
        self.explanation.setObjectName("" if plan.feasible else "Danger")
        self.explanation.setStyleSheet("" if plan.feasible else f"color: {theme.ACTIVE.danger};")

        self._show_progress(plan)

        rows = sorted(plan.categories, key=lambda c: -c.baseline.minor)
        self.table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            delta = item.allowance.minor - item.baseline.minor
            change = "-" if not delta else ("+" if delta > 0 else "−") + Money(abs(delta)).format()
            cells = [
                SortableItem(item.category, item.category.lower()),
                SortableItem(item.baseline.format(), item.baseline.minor),
                SortableItem(item.allowance.format(), item.allowance.minor),
                SortableItem(change, delta),
            ]
            for column, cell in enumerate(cells):
                if column:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
            if item.committed:
                # A committed cost is shown but greyed: it is part of the
                # arithmetic and not something the user is being asked to cut.
                for cell in cells:
                    cell.setForeground(Qt.GlobalColor.gray)
            for column, cell in enumerate(cells):
                self.table.setItem(index, column, cell)

    def _show_progress(self, plan) -> None:
        """How this period is going against the plan, so far."""
        report = self.ledger.budget_progress(plan)
        if report is None:
            self.progress.setVisible(False)
            self.progress_label.setText("")
            return

        spent, allowed, on_track = report
        self.progress.setVisible(True)
        self.progress.setMaximum(max(allowed.minor, 1))
        self.progress.setValue(min(spent.minor, allowed.minor))
        self.progress.setFormat(f"{spent.format()} of {allowed.format()} this period")
        colour = theme.ACTIVE.accent if on_track else theme.ACTIVE.danger
        self.progress.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {theme.ACTIVE.border}; border-radius: 6px; "
            f"height: 18px; text-align: center; }} "
            f"QProgressBar::chunk {{ background: {colour}; border-radius: 5px; }}"
        )
        self.progress_label.setText(
            "On track for this period." if on_track else "Over the allowance for this period."
        )
