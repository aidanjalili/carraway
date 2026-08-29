"""Net worth over time.

Drawn as a filled line rather than a table because the shape is the point: a
person wants to see whether the line goes up, and roughly when it did not.

The chart is hand-drawn with QPainter rather than a charting library. It is a
few dozen lines for one series, it inherits the app's palette automatically,
and it keeps the promise that the only runtime dependency is Qt.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...analysis import networth
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import Card, SortableItem, StatCard, StatRow


class Sparkline(QWidget):
    """A filled line chart of one money series."""

    def __init__(self) -> None:
        super().__init__()
        self.points: list[networth.NetWorthPoint] = []
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points: list[networth.NetWorthPoint]) -> None:
        self.points = points
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = theme.ACTIVE

        if len(self.points) < 2:
            painter.setPen(QColor(palette.muted))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "Not enough history to chart yet"
            )
            return

        margin = 12
        width = self.width() - margin * 2
        height = self.height() - margin * 2
        values = [p.net.minor for p in self.points]
        low, high = min(values), max(values)
        # A flat series would divide by zero; give it a band so the line sits
        # in the middle rather than on an edge.
        span = (high - low) or max(abs(high), 1)

        def place(index: int, value: int) -> QPointF:
            x = margin + width * index / (len(values) - 1)
            y = margin + height * (1 - (value - low) / span)
            return QPointF(x, y)

        line = QPainterPath(place(0, values[0]))
        for index, value in enumerate(values[1:], start=1):
            line.lineTo(place(index, value))

        # Fill under the line, so the eye reads volume rather than just slope.
        area = QPainterPath(line)
        area.lineTo(QPointF(margin + width, margin + height))
        area.lineTo(QPointF(margin, margin + height))
        area.closeSubpath()

        rising = values[-1] >= values[0]
        colour = QColor(palette.accent if rising else palette.danger)
        fill = QColor(colour)
        fill.setAlpha(38)
        painter.fillPath(area, fill)
        painter.setPen(QPen(colour, 2))
        painter.drawPath(line)

        # Zero matters on a net worth chart: above it is savings, below is debt.
        if low < 0 < high:
            zero_y = margin + height * (1 - (0 - low) / span)
            painter.setPen(QPen(QColor(palette.border), 1, Qt.PenStyle.DashLine))
            painter.drawLine(margin, int(zero_y), margin + width, int(zero_y))


_HEADERS = ["Date", "Assets", "Owed", "Net worth", "Change"]


class NetWorthView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("Net worth")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.granularity = QComboBox()
        self.granularity.addItems(["monthly", "weekly", "daily"])
        self.granularity.currentTextChanged.connect(lambda _: self.refresh())
        header.addWidget(self.granularity)
        layout.addLayout(header)

        self.net_card = StatCard("Net worth", "-", tone="Accent")
        self.assets_card = StatCard("Assets", "-")
        self.owed_card = StatCard("Owed", "-")
        self.change_card = StatCard("Change", "-")
        layout.addWidget(
            StatRow([self.net_card, self.assets_card, self.owed_card, self.change_card])
        )

        chart_card = Card()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(10, 10, 10, 10)
        self.chart = Sparkline()
        chart_layout.addWidget(self.chart)
        layout.addWidget(chart_card, stretch=1)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(230)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self.refresh()

    def refresh(self) -> None:
        points = self.ledger.networth_points(self.granularity.currentText())
        self.chart.set_points(points)

        if not points:
            self.footnote.setText(
                "Net worth needs a known balance to work back from. "
                "Run 'carraway sync simplefin' to record one."
            )
            self.table.setRowCount(0)
            return

        latest = points[-1]
        self.net_card.set_value(latest.net.format())
        self.assets_card.set_value(latest.assets.format())
        self.owed_card.set_value(latest.liabilities.format())

        summary = networth.summarise(points)
        sign = "+" if summary.change.minor >= 0 else "-"
        self.change_card.set_value(f"{sign}{abs(summary.change).format()}")

        recent = points[-24:]
        self.table.setRowCount(len(recent))
        for row, point in enumerate(reversed(recent)):
            index = len(recent) - 1 - row
            delta = point.net.minor - recent[index - 1].net.minor if index > 0 else 0
            cells = [
                SortableItem(point.date.isoformat(), point.date.toordinal()),
                SortableItem(point.assets.format(), point.assets.minor),
                SortableItem(point.liabilities.format(), point.liabilities.minor),
                SortableItem(point.net.format(), point.net.minor),
                SortableItem(
                    ("+" if delta >= 0 else "-") + Money(abs(delta)).format() if index else "-",
                    delta,
                ),
            ]
            for column, cell in enumerate(cells):
                if column:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 4 and index:
                    cell.setForeground(
                        QColor(theme.ACTIVE.accent if delta >= 0 else theme.ACTIVE.danger)
                    )
                self.table.setItem(row, column, cell)

        notes = [f"{len(points)} points, {points[0].date} to {points[-1].date}"]
        if summary.percent_change is not None:
            notes.append(f"{summary.percent_change:+.1f}% over the period")
        if summary.best_month:
            notes.append(f"best month {summary.best_month[0]} ({summary.best_month[1].format()})")
        missing = self.ledger.accounts_without_balances()
        if missing:
            # Ids come back, not accounts, so they are resolved to names here:
            # "excluded: 518742a0bfbf" tells the user nothing. Silently
            # dropping an account would misstate net worth by a constant,
            # which is worse than naming the gap.
            by_id = {a.id: a.name for a in self.ledger.accounts}
            names = [by_id.get(account_id, account_id) for account_id in missing]
            notes.append("excluded, no balance known: " + ", ".join(names))
        self.footnote.setText("   ·   ".join(notes))
