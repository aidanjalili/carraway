"""One budget, and how it is going.

The screen someone opens a week after setting a budget, so it is built around
the questions asked at that moment rather than the ones asked while setting it:

    Am I ahead or behind?
    How much is left in the one category I am about to spend from?
    How much per day can I still spend without blowing it?

That last pair is the point. "You have $612 left for Travel, over 12 days" is
what turns a budget into something you can hold a flight price up against —
which is not a thing a monthly average has ever been able to answer.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import (
    MeterDelegate,
    SortableItem,
    StatCard,
    StatRow,
    refresh_everything,
)

_HEADERS = ["Category", "Allowance", "Spent", "Left", "", "How it is going"]


class BudgetDetailView(QWidget):
    """A single saved budget, checked against what has actually been spent."""

    def __init__(self, ledger: Ledger, budget_id: str) -> None:
        super().__init__()
        self.ledger = ledger
        self.budget_id = budget_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("Title")
        header.addWidget(self.title)
        header.addStretch(1)
        self.delete_button = QPushButton("Delete this budget…")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._delete)
        header.addWidget(self.delete_button)
        layout.addLayout(header)

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("Subtitle")
        layout.addWidget(self.subtitle)

        # The three horizons a person actually decides on, then the pace
        # that says whether the month as a whole is going to hold. "Left this
        # month" alone does not answer "can I buy this now".
        self.left_card = StatCard("Left today", "-")
        self.week_card = StatCard("Left this week", "-")
        self.month_card = StatCard("Left in all", "-")
        self.spent_card = StatCard("Spent so far", "-")
        self.perday_card = StatCard("Left per day", "-")
        self.pace_card = StatCard("Pace", "-")
        layout.addWidget(StatRow([self.left_card, self.week_card, self.month_card]))
        # The context row: what has gone, the rate that would just hold, and
        # where the month should be by now.
        layout.addWidget(StatRow([self.spent_card, self.perday_card, self.pace_card]))

        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        layout.addWidget(self.verdict)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        # One delegate for the whole view, so every column hovers as one row;
        # the meter column reads a fraction out of UserRole and draws a bar.
        self._meter = MeterDelegate(self.table)
        self.table.setItemDelegate(self._meter)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 130)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignRight if column in (1, 2, 3) else Qt.AlignmentFlag.AlignLeft
            ) | Qt.AlignmentFlag.AlignVCenter
            self.table.horizontalHeaderItem(column).setTextAlignment(align)
        layout.addWidget(self.table, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self.refresh()

    # -- drawing ----------------------------------------------------------

    def refresh(self) -> None:
        budget = self.ledger.budget_by_id(self.budget_id)
        if budget is None:
            # Deleted from under us. The sidebar is about to be rebuilt, so
            # say something honest rather than crash on a missing record.
            self.title.setText("Budget deleted")
            self.subtitle.setText("")
            self.table.setRowCount(0)
            return

        state = self.ledger.budget_status(budget)
        self.title.setText(budget.name)
        self.subtitle.setText(self._describe(budget, state))

        # Shown even when negative on the shorter horizons: "you are $12 past
        # today's share" is something a person can act on this afternoon,
        # unlike a per-day figure that has gone negative for the whole month.
        left_today = state.left_today
        left_week = state.left_this_week
        self.left_card.set_value(left_today.format() if left_today is not None else "—")
        self.week_card.set_value(left_week.format() if left_week is not None else "—")
        self.month_card.set_value(state.remaining.format())
        self.spent_card.set_value(f"{state.spent.format()}  ({state.spent_today.format()} today)")
        # A negative allowance per day is not a number anyone can act on —
        # "you may spend -$70.53 today" is noise. The verdict line above
        # already says how far over it is.
        per_day = state.daily_remaining
        self.perday_card.set_value(per_day.format() if per_day and per_day.minor > 0 else "—")
        self.pace_card.set_value(state.pace.format())

        self._draw_verdict(state)
        self._draw_rows(state)
        self._draw_footnote(budget, state)

    def _describe(self, budget, state) -> str:
        span = f"{budget.starts_on.isoformat()} to {budget.ends_on.isoformat()}"
        if not state.started:
            days = (budget.starts_on - state.asof).days
            when = f"starts in {days} day{'s' if days != 1 else ''}"
        elif state.finished:
            when = "finished"
        else:
            when = f"day {state.elapsed_days} of {state.total_days} · {state.days_left} left"
        if budget.accounts:
            names = ", ".join(self.ledger.account_name(a) for a in budget.accounts)
            scope = f"counting {names}"
        else:
            scope = "counting every account"
        return f"{span}  ·  {when}  ·  {scope}"

    def _draw_verdict(self, state) -> None:
        """One sentence at the top saying whether to worry."""
        if not state.started:
            self.verdict.setObjectName("Muted")
            self.verdict.setText(
                f"Nothing spent yet. {state.allowance.format()} across "
                f"{state.total_days} days is {self._per_day(state).format()} a day."
            )
        elif state.remaining.minor < 0:
            self.verdict.setObjectName("Danger")
            self.verdict.setText(
                f"Over budget by {abs(state.remaining).format()}."
                + (
                    f" There are still {state.days_left} days to go."
                    if state.days_left
                    else " The window has closed."
                )
            )
        elif state.on_track:
            self.verdict.setObjectName("Accent")
            under = Money(state.pace.minor - state.spent.minor, state.spent.currency)
            text = f"On track — {under.format()} under where you would need to be by now."
            # The total can be under pace while most lines are over it, when
            # one big untouched category carries the rest. Saying only "on
            # track" there is true and misleading, so the count comes too.
            behind = [line for line in state.overspent if not line.unbudgeted]
            if behind:
                worst = behind[0]
                text += (
                    f" But {len(behind)} categories are over their line, "
                    f"{worst.category} worst by {abs(worst.remaining).format()}."
                )
            self.verdict.setText(text)
        else:
            self.verdict.setObjectName("Danger")
            over = Money(state.spent.minor - state.pace.minor, state.spent.currency)
            self.verdict.setText(
                f"Running hot: {over.format()} ahead of pace. Keeping this up spends "
                f"{self._projected(state).format()} by {state.budget.ends_on.isoformat()}."
            )
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)

    def _per_day(self, state) -> Money:
        days = max(state.total_days, 1)
        return Money(state.allowance.minor // days, state.allowance.currency)

    def _projected(self, state) -> Money:
        """What this rate spends by the end, if it does not change."""
        if not state.elapsed_days:
            return state.spent
        rate = state.spent.minor / state.elapsed_days
        return Money(int(rate * state.total_days), state.spent.currency)

    def _draw_rows(self, state) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(state.lines))
        for row, line in enumerate(state.lines):
            cells = [
                SortableItem(
                    line.category + ("  (not budgeted)" if line.unbudgeted else ""),
                    line.category.lower(),
                ),
                SortableItem(
                    line.allowance.format() if line.allowance.minor else "—",
                    line.allowance.minor,
                ),
                SortableItem(line.spent.format(), line.spent.minor),
                SortableItem(
                    line.remaining.format() if line.allowance.minor else "—",
                    line.remaining.minor,
                ),
                SortableItem("", float(line.fraction_used)),
                SortableItem(self._line_verdict(line, state), float(line.fraction_used)),
            ]
            # The meter column carries its fill as a fraction; see MeterDelegate.
            cells[4].setData(Qt.ItemDataRole.UserRole, float(line.fraction_used))

            for column, cell in enumerate(cells):
                if column in (1, 2, 3):
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if (line.over or line.unbudgeted) and column in (0, 3, 5):
                    cell.setForeground(QColor(theme.ACTIVE.danger))
                self.table.setItem(row, column, cell)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)

    def _line_verdict(self, line, state) -> str:
        """What this one category means for a decision today."""
        if line.unbudgeted:
            return "spent with no allowance set"
        if line.over:
            return f"over by {abs(line.remaining).format()}"
        if state.finished:
            return f"finished {line.remaining.format()} under"
        if state.days_left:
            per_day = Money(line.remaining.minor // state.days_left, line.remaining.currency)
            pace = "on track" if line.on_track else "ahead of pace"
            return f"{pace} · {line.remaining.format()} left, {per_day.format()}/day"
        return f"{line.remaining.format()} left"

    def _draw_footnote(self, budget, state) -> None:
        notes = []
        unbudgeted = [line for line in state.lines if line.unbudgeted]
        if unbudgeted:
            total = Money.zero()
            for line in unbudgeted:
                total = total + line.spent
            notes.append(
                f"{total.format()} spent in {len(unbudgeted)} categories this budget "
                "does not cover — real money, so it is counted in the totals"
            )
        if budget.expected_income is not None:
            saving = budget.savings_target or Money.zero()
            fixed = budget.fixed_costs or Money.zero()
            notes.append(
                f"built from {budget.expected_income.format()} income less "
                f"{saving.format()} saved and {fixed.format()} fixed"
            )
        self.footnote.setText("   ·   ".join(notes))

    # -- actions ----------------------------------------------------------

    def _delete(self) -> None:
        budget = self.ledger.budget_by_id(self.budget_id)
        if budget is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete this budget?",
            f"Delete “{budget.name}”?\n\nThis removes the budget and its "
            "allowances. Your transactions are not touched.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.ledger.delete_budget(self.budget_id)

        # And take it off the phone, which would otherwise go on showing an
        # allowance for a budget that no longer exists.
        from .pocket import publish_in_background

        publish_in_background(self, self.ledger)

        refresh_everything(self)


def today_default() -> date:  # pragma: no cover - kept for tests to patch
    return date.today()
