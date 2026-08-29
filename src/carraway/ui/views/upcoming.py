"""What is about to leave your account, and why we think so.

Every other screen looks backwards. This one is the only forward-looking view,
and it exists because the useful question about a subscription is rarely "what
did I pay" — it is "what is about to come out, and can I cover it".

Each row shows the evidence rather than just a prediction. A date derived from
eight charges landing on the 1st deserves more trust than one extrapolated from
two, and the user can only weigh that if they can see it.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...analysis import subscriptions
from ...core.money import Money, total
from .. import theme
from ..data import Ledger
from ..widgets import SortableItem, StatCard, StatRow, enable_row_hover

_HEADERS = ["When", "What", "Kind", "Amount", "Confidence", "Why we think so"]

_HORIZONS = {"next 7 days": 7, "next 14 days": 14, "next 30 days": 30, "next 90 days": 90}


def _why(series, ledger: Ledger) -> str:
    """The evidence behind a prediction, in a sentence."""
    if ledger.is_manual(series):
        return "you told Carraway about this one"

    cadence = series.cadence
    seen = series.occurrences
    if cadence == "monthly" and series.next_expected:
        return f"{seen} charges, most on the {series.next_expected.day}th of the month"
    if cadence in ("weekly", "biweekly"):
        return f"{seen} charges, roughly every {7 if cadence == 'weekly' else 14} days"
    return f"{seen} charges spaced {cadence}, last on {series.last_seen}"


class UpcomingView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Upcoming")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.horizon = QComboBox()
        self.horizon.addItems(list(_HORIZONS))
        self.horizon.setCurrentText("next 30 days")
        self.horizon.currentTextChanged.connect(lambda _: self.refresh())
        header.addWidget(self.horizon)
        layout.addLayout(header)

        subtitle = QLabel("Charges and deposits Carraway expects, and the evidence for each.")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(subtitle)

        self.out_card = StatCard("Going out", "-")
        self.in_card = StatCard("Coming in", "-")
        self.net_card = StatCard("Net", "-")
        self.count_card = StatCard("Expected", "-")
        layout.addWidget(StatRow([self.out_card, self.in_card, self.net_card, self.count_card]))

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # Mouse tracking so the row under the cursor repaints without a
        # click; without it Qt only updates on press.
        # Row-wide hover; Qt's stylesheet :hover only covers one cell.
        self._hover = enable_row_hover(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self.refresh()

    def _expected(self, horizon_days: int) -> list[tuple[date, object]]:
        """(date, series) for everything due inside the horizon, soonest first.

        A yearly charge inside a 30-day window appears once; a weekly one
        appears four times, because that is what will actually happen.
        """
        today = date.today()
        limit = today + timedelta(days=horizon_days)
        step = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91, "yearly": 365}

        out: list[tuple[date, object]] = []
        for series in self.ledger.series:
            if self.ledger.kind_of(series) == subscriptions.CANCELLED:
                continue
            when = series.next_expected
            if when is None:
                continue
            # A prediction already in the past is not upcoming; roll it forward
            # so a series that was missed by a few days still shows its next
            # real occurrence rather than disappearing.
            gap = step.get(series.cadence, 30)
            while when < today:
                when += timedelta(days=gap)
            while when <= limit:
                out.append((when, series))
                when += timedelta(days=gap)
        out.sort(key=lambda row: (row[0], -abs(row[1].typical_amount.minor)))
        return out

    def refresh(self) -> None:
        horizon = _HORIZONS[self.horizon.currentText()]
        rows = self._expected(horizon)
        today = date.today()

        outflows = [s.typical_amount for _, s in rows if s.typical_amount.minor < 0]
        inflows = [s.typical_amount for _, s in rows if s.typical_amount.minor > 0]
        going_out = total([abs(a) for a in outflows]) if outflows else Money.zero()
        coming_in = total(inflows) if inflows else Money.zero()

        self.out_card.set_value(going_out.format())
        self.in_card.set_value(coming_in.format())
        self.net_card.set_value(
            Money(coming_in.minor - going_out.minor, going_out.currency).format()
        )
        self.count_card.set_value(str(len(rows)))

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, (when, series) in enumerate(rows):
            days = (when - today).days
            when_text = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days}d")
            kind = self.ledger.kind_of(series)
            amount = self.ledger.current_amount(series)

            cells = [
                SortableItem(f"{when.isoformat()}  ({when_text})", when.toordinal()),
                SortableItem(series.merchant[:34], series.merchant.lower()),
                SortableItem(kind, kind),
                SortableItem(abs(amount).format(), abs(amount.minor)),
                SortableItem(f"{series.confidence:.0%}", series.confidence),
                SortableItem(_why(series, self.ledger), series.occurrences),
            ]
            for column, cell in enumerate(cells):
                if column in (3, 4):
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                # Anything due within the week is the part worth acting on.
                if days <= 7 and column == 0:
                    cell.setForeground(QColor(theme.ACTIVE.warning))
                if series.typical_amount.minor > 0 and column == 3:
                    cell.setForeground(QColor(theme.ACTIVE.accent))
                self.table.setItem(index, column, cell)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)

        thin = [s for _, s in rows if s.confidence < 0.7]
        notes = [f"{len(rows)} expected in the {self.horizon.currentText()}"]
        if thin:
            notes.append(f"{len(thin)} below 70% confidence — treat those as a maybe")
        self.footnote.setText("   ·   ".join(notes))
