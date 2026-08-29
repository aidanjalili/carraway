"""The subscriptions screen — the reason this project exists.

Everything else in a money app has an open source equivalent already. This
screen does not: a plain list of what recurs, what it costs a year, when the
next charge lands, and which ones look like they quietly stopped.

The headline number is the annual figure rather than the monthly one, because
$9.99 a month reads as nothing and $119.88 a year reads as a decision.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.models import RecurringSeries
from ...core.money import total
from ..data import Ledger
from ..widgets import SortableItem, StatCard, StatRow

_HEADERS = [
    "Merchant",
    "Kind",
    "Cadence",
    "Amount",
    "Per year",
    "Next charge",
    "Seen",
    "Confidence",
]

# Sort order for the Kind column: what you can cancel first, what you have
# not yet decided about last, since that is the row needing an action.
_KIND_ORDER = {"subscription": 0, "bill": 1, "habit": 2, "unknown": 3}


def _cadence_label(series: RecurringSeries) -> str:
    # A biweekly charge is 26 payments a year, not 24. People consistently
    # underestimate these, so the count is spelled out rather than implied.
    per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
    count = per_year.get(series.cadence, 0)
    suffix = " ~" if series.amount_varies else ""
    return f"{series.cadence} ({count}x){suffix}" if count else series.cadence


class SubscriptionsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("Subscriptions")
        title.setObjectName("Title")
        subtitle = QLabel("Everything that charges you on a schedule.")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.count_card = StatCard("Subscriptions", "0")
        self.monthly_card = StatCard("Per month", "-")
        self.yearly_card = StatCard("Per year", "-", tone="Accent")
        self.stale_card = StatCard("Unclassified", "0")
        layout.addWidget(
            StatRow([self.count_card, self.monthly_card, self.yearly_card, self.stale_card])
        )

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignLeft if column < 3 else Qt.AlignmentFlag.AlignRight
            ) | Qt.AlignmentFlag.AlignVCenter
            self.table.horizontalHeaderItem(column).setTextAlignment(align)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        layout.addWidget(self.footnote)

        self.refresh()

    def refresh(self) -> None:
        series = self.ledger.series
        active = self.ledger.active_series
        stale = self.ledger.stale_series
        today = date.today()

        # The headline is what the user could actually cancel. Rent and
        # utilities recur just as reliably and belong in a different column of
        # someone's thinking, so they are counted separately.
        cancellable = [s for s in active if self.ledger.kind_of(s) == "subscription"]
        unknown = [s for s in self.ledger.series if self.ledger.kind_of(s) == "unknown"]

        self.count_card.set_value(str(len(cancellable)))
        self.monthly_card.set_value(self.ledger.monthly_cost(cancellable).format())
        self.yearly_card.set_value(total([s.annualised for s in cancellable]).format())
        self.stale_card.set_value(str(len(unknown)))

        # Sorting has to be off while filling, or rows reorder underneath the
        # loop and land in the wrong places.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(series))
        stale_ids = {id(s) for s in stale}

        for row, item in enumerate(series):
            gone = id(item) in stale_ids
            name = item.merchant + ("  (stopped?)" if gone else "")
            kind = self.ledger.kind_of(item)
            cells = [
                SortableItem(name, item.merchant.lower()),
                SortableItem(kind, _KIND_ORDER.get(kind, 9)),
                SortableItem(_cadence_label(item), item.annualised.minor),
                SortableItem(abs(item.typical_amount).format(), abs(item.typical_amount.minor)),
                SortableItem(item.annualised.format(), item.annualised.minor),
                SortableItem(
                    item.next_expected.isoformat() if item.next_expected else "-",
                    item.next_expected.toordinal() if item.next_expected else 0,
                ),
                SortableItem(str(item.occurrences), item.occurrences),
                SortableItem(f"{item.confidence:.0%}", item.confidence),
            ]
            for column, cell in enumerate(cells):
                if column >= 3:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if gone:
                    cell.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(row, column, cell)

        self.table.setSortingEnabled(True)
        self.table.sortItems(4, Qt.SortOrder.DescendingOrder)

        varies = sum(1 for s in series if s.amount_varies)
        notes = [f"{len(series)} series detected as of {today.isoformat()}"]
        if unknown:
            notes.append(f"{len(unknown)} unclassified — run 'carraway review'")
        if varies:
            notes.append(f"~ marks {varies} whose amount changes between charges")
        if stale:
            notes.append(f"{len(stale)} greyed out — expected charge never arrived")
        self.footnote.setText("   ·   ".join(notes))
