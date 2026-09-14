"""Net worth over time.

Drawn as a filled line rather than a table because the shape is the point: a
person wants to see whether the line goes up, and roughly when it did not.

The chart is hand-drawn with QPainter rather than a charting library. It is a
few dozen lines for one series, it inherits the app's palette automatically,
and it keeps the promise that the only runtime dependency is Qt.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...analysis import networth
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import (
    PanelSplitter,
    SortableItem,
    StatCard,
    StatRow,
    shorten,
)
from . import expected_money


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

_EXPECTED_HEADERS = ["Expected", "What it is", "Amount", "Landing in", "Note"]


def _split_flows(entries) -> tuple[Money, Money]:
    """(arriving, leaving), both as positive figures."""
    arriving = sum((e.amount.minor for e in entries if e.amount.minor > 0), 0)
    leaving = sum((-e.amount.minor for e in entries if e.amount.minor < 0), 0)
    currency = entries[0].amount.currency if entries else "USD"
    return Money(arriving, currency), Money(leaving, currency)


def _summarise_flows(entries) -> str:
    """The short form, for the line under the headline figure.

    Both halves are named whenever both exist. A single netted figure is the
    one thing this must not print: "$2,531.02 on its way" for $3,048 arriving
    and $517 going out is true arithmetic and a false description -- it hides
    the outgoing half completely, and this panel holds bills as readily as
    cheques.
    """
    arriving, leaving = _split_flows(entries)
    if arriving.minor and leaving.minor:
        return f"+{arriving.format()} in, -{leaving.format()} out"
    if leaving.minor:
        return f"-{leaving.format()} still to go out"
    return f"+{arriving.format()} on its way"


def _summarise_flows_long(entries) -> str:
    arriving, leaving = _split_flows(entries)
    if arriving.minor and leaving.minor:
        return f"{arriving.format()} coming in and {leaving.format()} going out"
    if leaving.minor:
        return f"{leaving.format()} still to go out"
    return f"{arriving.format()} on its way"


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
        layout.addWidget(StatRow([self.net_card, self.assets_card, self.owed_card]))

        self.chart = NetWorthChart()

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # No maximum height any more: the splitter decides how tall this is,
        # and a cap would quietly fight whatever the user dragged it to.
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        # A splitter rather than three stacked widgets, so the three can be
        # given whatever share of the screen the question of the moment wants:
        # the chart while reading the shape, the table while reading figures.
        self.panels = PanelSplitter("networth", ledger, defaults=(5, 3, 3))
        self.panels.add_panel("Over time", self.chart)
        self.panels.add_panel("Point by point", self.table)
        self.panels.add_panel("On its way", self._build_expected_body())
        layout.addWidget(self.panels, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self._build_account_toggles()
        self.panels.restore()
        self.refresh()

    # -- money that has not landed yet -------------------------------------

    def _build_expected_body(self) -> QWidget:
        """Things the bank has not seen, listed under the real figures.

        Below the chart and the history rather than beside the headline,
        because everything above this point is what the bank says and this is
        not. Keeping them in that order is the whole reason a projected net
        worth can be shown at all without muddying the real one.
        """
        card = QWidget()
        inner = QVBoxLayout(card)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(9)

        head = QHBoxLayout()
        head.addStretch(1)
        self.expected_add = QPushButton("Add")
        self.expected_add.clicked.connect(self._add_expected)
        head.addWidget(self.expected_add)
        self.expected_remove = QPushButton("It landed — remove")
        self.expected_remove.setEnabled(False)
        self.expected_remove.clicked.connect(self._remove_expected)
        head.addWidget(self.expected_remove)
        inner.addLayout(head)

        self.expected_blurb = QLabel("")
        self.expected_blurb.setObjectName("Muted")
        self.expected_blurb.setWordWrap(True)
        inner.addWidget(self.expected_blurb)

        self.expected_table = QTableWidget(0, len(_EXPECTED_HEADERS))
        self.expected_table.setHorizontalHeaderLabels(_EXPECTED_HEADERS)
        self.expected_table.verticalHeader().setVisible(False)
        self.expected_table.setAlternatingRowColors(True)
        self.expected_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.expected_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.expected_table.setMaximumHeight(160)
        head_view = self.expected_table.horizontalHeader()
        head_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            head_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.expected_table.itemSelectionChanged.connect(self._expected_selection_changed)
        # Right-click to correct one. A mistyped date is the common mistake
        # here, and without this the only way to fix it is to delete the entry
        # and type the whole thing again.
        self.expected_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.expected_table.customContextMenuRequested.connect(self._expected_menu)
        # Double-click does the same, because that is what a row in a table
        # that can be edited is expected to do.
        self.expected_table.doubleClicked.connect(lambda _: self._edit_expected())
        inner.addWidget(self.expected_table)
        return card

    def _expected_menu(self, position) -> None:
        row = self.expected_table.indexAt(position).row()
        if row < 0:
            return
        # Right-clicking a row nobody selected should act on that row, not on
        # whatever was selected before it.
        self.expected_table.selectRow(row)

        menu = QMenu(self)
        edit = QAction("Edit…", self)
        edit.triggered.connect(self._edit_expected)
        menu.addAction(edit)
        remove = QAction("It landed — remove", self)
        remove.triggered.connect(self._remove_expected)
        menu.addAction(remove)
        menu.exec(self.expected_table.viewport().mapToGlobal(position))

    def _edit_expected(self) -> None:
        chosen = self._selected_expected()
        if len(chosen) != 1:
            return
        entry = chosen[0]
        points = self.ledger.networth_points(self.granularity.currentText())
        current = points[-1].net if points else None
        values = expected_money.prompt(self.ledger.accounts, current, self, entry)
        if values is None:
            return
        self.ledger.update_expected_money(
            entry.id,
            values["description"],
            values["amount"],
            expected_on=values["expected_on"],
            account_id=values["account_id"],
            note=values["note"],
        )
        from ..widgets import refresh_everything

        refresh_everything(self)

    def _expected_selection_changed(self) -> None:
        self.expected_remove.setEnabled(bool(self.expected_table.selectedItems()))

    def _selected_expected(self):
        rows = {i.row() for i in self.expected_table.selectedIndexes()}
        entries = self.ledger.expected_money()
        return [entries[r] for r in sorted(rows) if r < len(entries)]

    def _add_expected(self) -> None:
        points = self.ledger.networth_points(self.granularity.currentText())
        # The figure the dialog previews against is the real one, before
        # anything already written down is added -- otherwise the preview
        # compounds the last entry into the next one.
        current = points[-1].net if points else None
        values = expected_money.prompt(self.ledger.accounts, current, self)
        if values is None:
            return
        self.ledger.add_expected_money(
            values["description"],
            values["amount"],
            expected_on=values["expected_on"],
            account_id=values["account_id"],
            note=values["note"],
        )
        from ..widgets import refresh_everything

        refresh_everything(self)

    def _remove_expected(self) -> None:
        chosen = self._selected_expected()
        if not chosen:
            return
        names = ", ".join(e.description for e in chosen[:3])
        confirm = QMessageBox.question(
            self,
            "Remove from what is on its way?",
            f"{names} will stop counting towards your projected net worth.\n\n"
            "Do this once the money has actually landed — the real transaction "
            "will be in your ledger by then, and leaving this here would count "
            "it twice.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for entry in chosen:
            self.ledger.delete_expected_money(entry.id)
        from ..widgets import refresh_everything

        refresh_everything(self)

    def _refresh_expected(self, current_net) -> None:
        entries = self.ledger.expected_money()
        counted = {e.id for e in self.ledger.counted_expected_money()}
        names = {a.id: a.name for a in self.ledger.accounts}

        self.expected_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            where = names.get(entry.account_id, "") if entry.account_id else ""
            if entry.account_id and entry.id not in counted:
                # Said on the row rather than silently dropped from the
                # total. An entry landing in an account left out of net worth
                # would otherwise look like it was being counted and was not.
                where = f"{where or entry.account_id} (not counted)"
            cells = [
                SortableItem(
                    expected_money.describe(entry),
                    entry.expected_on.toordinal() if entry.expected_on else 10**7,
                ),
                QTableWidgetItem(entry.description),
                SortableItem(entry.amount.format(), entry.amount.minor),
                QTableWidgetItem(where),
                QTableWidgetItem(entry.note),
            ]
            cells[2].setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            cells[2].setForeground(
                QColor(
                    theme.ACTIVE.accent if entry.amount.minor >= 0 else theme.ACTIVE.danger
                )
            )
            for column, cell in enumerate(cells):
                self.expected_table.setItem(row, column, cell)

        total = self.ledger.expected_total()
        counted = self.ledger.counted_expected_money()
        if not entries:
            self.expected_blurb.setText(
                "Nothing written down. Use this for money you are owed — a "
                "cheque in the post, a reimbursement — or for money you know "
                "is going out, like a bill that has not hit the statement yet. "
                "Net worth above keeps saying what your bank says; what has "
                "not landed is added separately."
            )
        elif current_net is None:
            self.expected_blurb.setText(
                f"{_summarise_flows_long(counted)}, but there is no known "
                "balance to add it to yet."
            )
        else:
            after = Money(current_net.minor + total.minor, current_net.currency)
            self.expected_blurb.setText(
                f"{_summarise_flows_long(counted)}. Net worth is "
                f"{current_net.format()} today and would be {after.format()} "
                "once it all lands."
            )
        self._expected_selection_changed()

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
            box = QCheckBox(shorten(account.name, 22))
            box.setChecked(account.id not in excluded)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setToolTip(self._account_tooltip(account))
            box.toggled.connect(
                lambda checked, account_id=account.id: self._toggle_account(account_id, checked)
            )
            self.account_boxes[account.id] = box
            self.include_row.insertWidget(self.include_row.count() - 1, box)

        if not self.account_boxes:
            self.include_label.setText("")

    def _account_tooltip(self, account) -> str:
        """What this account holds, and how it affects the total.

        The checkbox label is truncated to fit the row, so the tooltip carries
        the full name — and the figure, since the whole reason to hover a
        toggle is to decide whether to untick it, which needs to know what
        unticking would cost.
        """
        balance = self.ledger.balances.get(account.id)
        lines = [account.name]
        detail = account.institution or str(account.type)
        if detail and detail != account.name:
            lines.append(detail)

        if balance is None:
            lines.append("No balance recorded — not counted.")
            return "\n".join(lines)

        if account.type.is_liability:
            # A card balance arrives negative and means money owed, so it is
            # shown as a magnitude with the direction spelled out rather than
            # as a minus sign the reader has to interpret.
            lines.append(f"{abs(balance).format()} owed — subtracts from net worth")
        else:
            lines.append(f"{balance.format()} held — adds to net worth")

        if account.id in self.ledger.excluded_accounts:
            lines.append("Currently not counted.")
        return "\n".join(lines)

    def _toggle_account(self, account_id: str, included: bool) -> None:
        excluded = self.ledger.excluded_accounts
        if included:
            excluded.discard(account_id)
        else:
            excluded.add(account_id)
        self.ledger.save_setting("networth_excluded_accounts", sorted(excluded))
        for other_id, box in self.account_boxes.items():
            account = next((a for a in self.ledger.accounts if a.id == other_id), None)
            if account is not None:
                box.setToolTip(self._account_tooltip(account))
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
            # Still drawn: something written down here is worth seeing even
            # when there is no balance to add it to, and disappearing without
            # explanation would read as having lost it.
            self._refresh_expected(None)
            self.net_card.set_comparison("")
            return

        latest = points[-1]
        self.net_card.set_value(latest.net.format())
        self._refresh_expected(latest.net)
        # Under the headline, never inside it. The big figure stays the one
        # the bank would agree with; this says what it becomes.
        counted = self.ledger.counted_expected_money()
        expected_total = self.ledger.expected_total()
        if counted:
            after = Money(latest.net.minor + expected_total.minor, latest.net.currency)
            self.net_card.set_comparison(
                f"{_summarise_flows(counted)} → {after.format()}",
                "Accent" if expected_total.minor >= 0 else "Danger",
            )
        else:
            self.net_card.set_comparison("")
        self.assets_card.set_value(latest.assets.format())
        self.owed_card.set_value(latest.liabilities.format())

        summary = networth.summarise(points)

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

        expected_count = len(self.ledger.expected_money())
        if expected_count:
            notes.append(
                f"{expected_count} thing{'' if expected_count == 1 else 's'} not landed "
                "yet, and not in the chart — the line is only what the bank has confirmed"
            )

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
