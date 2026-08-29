"""Net worth over time.

Drawn as a filled line rather than a table because the shape is the point: a
person wants to see whether the line goes up, and roughly when it did not.

The chart is hand-drawn with QPainter rather than a charting library. It is a
few dozen lines for one series, it inherits the app's palette automatically,
and it keeps the promise that the only runtime dependency is Qt.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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


class NetWorthChart(QWidget):
    """A filled line of net worth over time, with axes and a hover readout.

    Axes matter here in a way they do not on a sparkline: the question is
    "when did that dip happen and how deep was it", which needs both scales
    labelled. Hovering snaps to the nearest point rather than interpolating,
    because every point is a real reconstructed balance and a value between
    two of them is not.
    """

    # Room for the value labels on the left and date labels underneath.
    LEFT = 74
    BOTTOM = 26
    PAD = 14

    def __init__(self) -> None:
        super().__init__()
        self.points: list[networth.NetWorthPoint] = []
        self.setMinimumHeight(240)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._hovered: int | None = None
        self._plot = QRectF()
        self._low = 0
        self._span = 1

    def set_points(self, points: list[networth.NetWorthPoint]) -> None:
        self.points = points
        self._hovered = None
        self.update()

    # -- geometry --------------------------------------------------------

    def _x(self, index: int) -> float:
        if len(self.points) < 2:
            return self._plot.left()
        return self._plot.left() + self._plot.width() * index / (len(self.points) - 1)

    def _y(self, value: int) -> float:
        return self._plot.bottom() - self._plot.height() * (value - self._low) / self._span

    def _nearest(self, x: float) -> int | None:
        if len(self.points) < 2 or not self._plot.width():
            return None
        ratio = (x - self._plot.left()) / self._plot.width()
        index = round(ratio * (len(self.points) - 1))
        return max(0, min(len(self.points) - 1, index))

    # -- interaction -----------------------------------------------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._nearest(event.position().x())
        if index != self._hovered:
            self._hovered = index
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = None
        self.update()

    # -- painting --------------------------------------------------------

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

        self._plot = QRectF(
            self.LEFT,
            self.PAD,
            max(10.0, self.width() - self.LEFT - self.PAD),
            max(10.0, self.height() - self.PAD - self.BOTTOM),
        )
        values = [p.net.minor for p in self.points]
        low, high = min(values), max(values)
        # A little headroom, so the extremes are not drawn on the frame.
        margin = max((high - low) // 12, 100)
        self._low = low - margin
        self._span = (high + margin) - self._low or 1

        self._draw_value_axis(painter, palette)
        self._draw_date_axis(painter, palette)

        line = QPainterPath(QPointF(self._x(0), self._y(values[0])))
        for index, value in enumerate(values[1:], start=1):
            line.lineTo(QPointF(self._x(index), self._y(value)))

        area = QPainterPath(line)
        area.lineTo(QPointF(self._x(len(values) - 1), self._plot.bottom()))
        area.lineTo(QPointF(self._x(0), self._plot.bottom()))
        area.closeSubpath()

        rising = values[-1] >= values[0]
        colour = QColor(palette.accent if rising else palette.danger)
        fill = QColor(colour)
        fill.setAlpha(38)
        painter.fillPath(area, fill)
        painter.setPen(QPen(colour, 2))
        painter.drawPath(line)

        if self._hovered is not None:
            self._draw_hover(painter, palette, colour)

    def _draw_value_axis(self, painter: QPainter, palette) -> None:
        """Four gridlines with money labels, which is enough to read a level."""
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        for step in range(5):
            value = self._low + self._span * step / 4
            y = self._y(int(value))
            painter.setPen(QPen(QColor(palette.border), 1, Qt.PenStyle.DotLine))
            painter.drawLine(int(self._plot.left()), int(y), int(self._plot.right()), int(y))
            painter.setPen(QColor(palette.muted))
            painter.drawText(
                QRectF(0, y - 9, self.LEFT - 8, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                Money(int(value)).format(symbol=True),
            )

    def _draw_date_axis(self, painter: QPainter, palette) -> None:
        """As many date labels as fit without colliding."""
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(palette.muted))

        # One label per ~90px; a monthly series over two years is 26 points
        # and every one labelled would be unreadable.
        wanted = max(2, int(self._plot.width() // 90))
        stride = max(1, (len(self.points) - 1) // wanted)
        for index in range(0, len(self.points), stride):
            when = self.points[index].date
            painter.drawText(
                QRectF(self._x(index) - 40, self._plot.bottom() + 4, 80, 18),
                Qt.AlignmentFlag.AlignCenter,
                when.strftime("%b %Y") if stride > 1 else when.isoformat(),
            )

    def _draw_hover(self, painter: QPainter, palette, colour: QColor) -> None:
        """A crosshair, a marker and a readout for the point under the cursor."""
        index = self._hovered
        point = self.points[index]
        x, y = self._x(index), self._y(point.net.minor)

        painter.setPen(QPen(QColor(palette.muted), 1, Qt.PenStyle.DashLine))
        painter.drawLine(int(x), int(self._plot.top()), int(x), int(self._plot.bottom()))

        painter.setPen(QPen(QColor(palette.surface), 2))
        painter.setBrush(colour)
        painter.drawEllipse(QPointF(x, y), 5, 5)

        change = ""
        if index > 0:
            delta = point.net.minor - self.points[index - 1].net.minor
            change = f"   {'+' if delta >= 0 else '−'}{Money(abs(delta)).format()}"
        label = f"{point.date.isoformat()}   {point.net.format()}{change}"

        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(label) + 18
        # Flip the box to the other side near the right edge, so it never runs
        # off the widget.
        left = x + 12 if x + 12 + width < self._plot.right() else x - 12 - width
        box = QRectF(left, self._plot.top() + 6, width, 26)

        painter.setPen(QPen(QColor(palette.border), 1))
        painter.setBrush(QColor(palette.surface))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor(palette.text))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)


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
        remembered = str(ledger.setting("networth_granularity") or "monthly")
        if remembered in ("monthly", "weekly", "daily"):
            self.granularity.setCurrentText(remembered)
        self.granularity.currentTextChanged.connect(lambda _: self.refresh())
        header.addWidget(self.granularity)
        layout.addLayout(header)

        # Pinned here rather than buried in Settings: "what is my net worth
        # excluding retirement?" is a question asked while looking at the
        # number, and a screen away is a screen too far. The same values are
        # editable in Settings for anyone who prefers them there.
        self.include_row = QHBoxLayout()
        self.include_row.setSpacing(10)
        self.include_label = QLabel("Counting:")
        self.include_label.setObjectName("Muted")
        self.include_row.addWidget(self.include_label)
        self.account_boxes: dict[str, QCheckBox] = {}
        self.include_row.addStretch(1)
        layout.addLayout(self.include_row)

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
        self.chart = NetWorthChart()
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

        self._build_account_toggles()
        self.refresh()

    def _build_account_toggles(self) -> None:
        """One checkbox per account, so the total can be recut on the spot."""
        for box in self.account_boxes.values():
            self.include_row.removeWidget(box)
            box.deleteLater()
        self.account_boxes = {}

        excluded = self.ledger.excluded_accounts
        # Only accounts with a balance can affect the total, so offering the
        # others would be a control that does nothing.
        for account in self.ledger.accounts:
            if account.id not in self.ledger.balances:
                continue
            box = QCheckBox(account.name[:22])
            box.setChecked(account.id not in excluded)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setToolTip(f"{account.name} — {account.institution or account.type}")
            box.toggled.connect(
                lambda checked, account_id=account.id: self._toggle_account(account_id, checked)
            )
            self.account_boxes[account.id] = box
            self.include_row.insertWidget(self.include_row.count() - 1, box)

        if not self.account_boxes:
            self.include_label.setText("")

    def _toggle_account(self, account_id: str, included: bool) -> None:
        excluded = self.ledger.excluded_accounts
        if included:
            excluded.discard(account_id)
        else:
            excluded.add(account_id)
        self.ledger.save_setting("networth_excluded_accounts", sorted(excluded))
        self.refresh()

    def refresh(self) -> None:
        # Remember the zoom between sessions; it is a preference, not a mode.
        self.ledger.save_setting("networth_granularity", self.granularity.currentText())
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
        excluded = self.ledger.excluded_accounts
        if excluded:
            by_id = {a.id: a.name for a in self.ledger.accounts}
            left_out = [by_id.get(i, i) for i in sorted(excluded)]
            notes.append("not counted: " + ", ".join(left_out))

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
