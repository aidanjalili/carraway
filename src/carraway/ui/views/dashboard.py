"""The overview screen: where the money went, at a glance.

Spending is drawn as proportional bars rather than a pie chart. A pie makes
adjacent slices almost impossible to compare, and comparing categories is the
entire job of this screen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.money import total
from ..data import Ledger
from ..widgets import Card, StatCard, StatRow


class _Bar(QFrame):
    """A single proportional bar. Width is set by the layout stretch factors."""

    def __init__(self, fraction: float, colour: str) -> None:
        super().__init__()
        self.setFixedHeight(8)
        self.setStyleSheet(f"background: {colour}; border-radius: 4px;")
        self.fraction = fraction


class DashboardView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(18)

        title = QLabel("Overview")
        title.setObjectName("Title")
        self.range_label = QLabel("")
        self.range_label.setObjectName("Subtitle")
        outer.addWidget(title)
        outer.addWidget(self.range_label)

        self.in_card = StatCard("Money in", "-")
        self.out_card = StatCard("Money out", "-")
        self.net_card = StatCard("Net", "-")
        self.tx_card = StatCard("Transactions", "-")
        outer.addWidget(StatRow([self.in_card, self.out_card, self.net_card, self.tx_card]))

        heading = QLabel("Spending by category")
        heading.setObjectName("SectionHeading")
        outer.addWidget(heading)

        self.categories_card = Card()
        self.categories_layout = QGridLayout(self.categories_card)
        self.categories_layout.setContentsMargins(20, 16, 20, 16)
        self.categories_layout.setHorizontalSpacing(14)
        self.categories_layout.setVerticalSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.categories_card)
        outer.addWidget(scroll, stretch=1)

        self.refresh()

    def refresh(self) -> None:
        txs = self.ledger.transactions
        if not txs:
            self.range_label.setText("No transactions imported yet.")
            return

        spent = total([t.amount for t in txs if t.is_outflow and not t.is_transfer])
        earned = total([t.amount for t in txs if not t.is_outflow and not t.is_transfer])
        dates = [t.date for t in txs]

        self.range_label.setText(f"{min(dates).isoformat()} to {max(dates).isoformat()}")
        self.in_card.set_value(earned.format())
        self.out_card.set_value(abs(spent).format())
        self.net_card.set_value((earned + spent).format())
        self.tx_card.set_value(f"{len(txs):,}")

        while self.categories_layout.count():
            item = self.categories_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        rows = self.ledger.spending_by_category()
        if not rows:
            return
        largest = rows[0][1].minor or 1

        # One accent hue, stepped in lightness. Distinct colours per category
        # would imply the categories are unrelated; they are all spending.
        for index, (name, amount, count) in enumerate(rows):
            fraction = amount.minor / largest
            shade = 45 + int(25 * (1 - fraction))

            label = QLabel(name)
            value = QLabel(amount.format())
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            note = QLabel(f"{count} txns")
            note.setObjectName("Muted")

            track = QWidget()
            track_layout = QVBoxLayout(track)
            track_layout.setContentsMargins(0, 0, 0, 0)
            bar_row = QWidget()
            bar_layout = QVBoxLayout(bar_row)
            bar_layout.setContentsMargins(0, 0, 0, 0)
            bar = _Bar(fraction, f"hsl(145, 55%, {shade}%)")
            bar.setMinimumWidth(max(6, int(240 * fraction)))
            bar.setMaximumWidth(max(6, int(240 * fraction)))
            bar_layout.addWidget(bar)
            track_layout.addWidget(bar_row)

            self.categories_layout.addWidget(label, index, 0)
            self.categories_layout.addWidget(track, index, 1)
            self.categories_layout.addWidget(value, index, 2)
            self.categories_layout.addWidget(note, index, 3)

        self.categories_layout.setColumnStretch(0, 2)
        self.categories_layout.setColumnStretch(1, 3)
