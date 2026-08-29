"""Where the money went, at whatever zoom you want.

One period at a time, steppable, with the same numbers shown as a pie, as bars
or as a table. The chart type is a genuine preference rather than a gimmick: a
pie reads shares, bars compare sizes accurately, and a table is the only one
you can copy a figure out of.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...analysis import spending
from ...core.money import Money
from ..data import Ledger
from ..widgets import Card, SortableItem, StatCard, StatRow, enable_row_hover
from .charts import BarChart, PieChart, Slice, TrendChart

_HEADERS = ["Category", "Spent", "Share", "Transactions"]


class SpendingView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self.buckets: list[spending.Bucket] = []
        self.index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Spending")
        title.setObjectName("Title")
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.granularity = QComboBox()
        self.granularity.addItems(["monthly", "weekly", "daily", "yearly"])
        self.granularity.currentTextChanged.connect(self._reload)
        controls.addWidget(QLabel("Show"))
        controls.addWidget(self.granularity)

        self.previous = QPushButton("‹")
        self.previous.setFixedWidth(34)
        self.previous.clicked.connect(lambda: self._step(-1))
        self.period_label = QLabel("")
        self.period_label.setObjectName("SectionHeading")
        self.period_label.setMinimumWidth(150)
        self.period_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next = QPushButton("›")
        self.next.setFixedWidth(34)
        self.next.clicked.connect(lambda: self._step(1))
        controls.addSpacing(12)
        controls.addWidget(self.previous)
        controls.addWidget(self.period_label)
        controls.addWidget(self.next)
        controls.addStretch(1)

        # Chart type as exclusive buttons rather than a dropdown: there are
        # three, and switching between them is the whole point of the screen.
        self.chart_buttons = QButtonGroup(self)
        self.chart_buttons.setExclusive(True)
        for position, name in enumerate(("Pie", "Bars", "Table", "Trend")):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(position == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.chart_buttons.addButton(button, position)
            controls.addWidget(button)
        self.chart_buttons.idClicked.connect(self._show_chart)
        layout.addLayout(controls)

        self.total_card = StatCard("Spent this period", "-")
        self.average_card = StatCard("Average per period", "-")
        self.biggest_card = StatCard("Biggest category", "-")
        self.count_card = StatCard("Transactions", "-")
        layout.addWidget(
            StatRow([self.total_card, self.average_card, self.biggest_card, self.count_card])
        )

        board = Card()
        board_layout = QVBoxLayout(board)
        board_layout.setContentsMargins(12, 12, 12, 12)

        self.pie = PieChart()
        self.bars = BarChart()
        self.trend = TrendChart()
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # Mouse tracking so the row under the cursor repaints without a
        # click; without it Qt only updates on press.
        # Row-wide hover; Qt's stylesheet :hover only covers one cell.
        self._hover = enable_row_hover(self.table)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        self.stack = QStackedWidget()
        for widget in (self.pie, self.bars, self.table, self.trend):
            self.stack.addWidget(widget)
        board_layout.addWidget(self.stack)
        layout.addWidget(board, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self._reload()

    # -- data ------------------------------------------------------------

    def _reload(self) -> None:
        period = self.granularity.currentText()
        self.buckets = self.ledger.spending_buckets(
            period, include_guessed=bool(self.ledger.setting("include_guesses_in_totals"))
        )
        # Land on the most recent period: that is what someone opening this
        # screen wants, not the oldest month in their history.
        self.index = len(self.buckets) - 1 if self.buckets else 0
        self.refresh()

    def _step(self, direction: int) -> None:
        if not self.buckets:
            return
        self.index = max(0, min(len(self.buckets) - 1, self.index + direction))
        self.refresh()

    def _show_chart(self, position: int) -> None:
        self.stack.setCurrentIndex(position)
        self.refresh()

    def refresh(self) -> None:
        if not self.buckets:
            self.period_label.setText("no data")
            self.footnote.setText("Import or sync some transactions to see this.")
            return

        bucket = self.buckets[self.index]
        rows = sorted(bucket.by_category.items(), key=lambda kv: -kv[1].minor)
        total = bucket.total
        slices = [
            Slice(
                label=name,
                amount=amount,
                fraction=(amount.minor / total.minor) if total.minor else 0.0,
            )
            for name, amount in rows
        ]

        self.pie.set_slices(slices)
        self.bars.set_slices(slices)
        # The trend chart ignores the selected period and shows every one, so
        # it answers "is this month unusual?" rather than "what was in it?".
        self.trend.set_slices(
            [Slice(label=b.label, amount=b.total, fraction=0.0) for b in self.buckets[-60:]]
        )
        self._fill_table(slices, total)

        self.period_label.setText(bucket.label)
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index < len(self.buckets) - 1)

        self.total_card.set_value(total.format())
        spent = [b.total for b in self.buckets if b.total.minor]
        average = (
            Money(round(sum(b.minor for b in spent) / len(spent)), total.currency)
            if spent
            else Money.zero()
        )
        self.average_card.set_value(average.format())
        self.biggest_card.set_value(rows[0][0] if rows else "-")
        self.count_card.set_value(str(self._transaction_count(bucket)))

        change = ""
        if self.index > 0:
            previous = self.buckets[self.index - 1]
            delta = total.minor - previous.total.minor
            if previous.total.minor:
                pct = 100 * delta / previous.total.minor
                direction = "more" if delta > 0 else "less"
                change = (
                    f"{Money(abs(delta), total.currency).format()} {direction} "
                    f"than {previous.label} ({pct:+.0f}%)"
                )
        parts = [f"{len(self.buckets)} periods on record"]
        if change:
            parts.append(change)
        self.footnote.setText("   ·   ".join(parts))

    def _transaction_count(self, bucket) -> int:
        return sum(
            1
            for t in self.ledger.transactions
            if bucket.start <= t.date < bucket.end and t.is_outflow and not t.is_transfer
        )

    def _fill_table(self, slices: list[Slice], total: Money) -> None:
        counts: dict[str, int] = {}
        bucket = self.buckets[self.index]
        for transaction in self.ledger.transactions:
            if not (bucket.start <= transaction.date < bucket.end):
                continue
            if not transaction.is_outflow or transaction.is_transfer:
                continue
            name = self.ledger.category_of(
                transaction,
                include_guessed=bool(self.ledger.setting("include_guesses_in_totals")),
            )
            counts[name] = counts.get(name, 0) + 1

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(slices))
        for row, item in enumerate(slices):
            cells = [
                SortableItem(item.label, item.label.lower()),
                SortableItem(item.amount.format(), item.amount.minor),
                SortableItem(f"{item.fraction:.1%}", item.fraction),
                SortableItem(str(counts.get(item.label, 0)), counts.get(item.label, 0)),
            ]
            for column, cell in enumerate(cells):
                if column:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, cell)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)
