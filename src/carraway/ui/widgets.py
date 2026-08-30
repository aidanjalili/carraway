"""Small shared widgets.

Kept separate so the screens stay mostly layout code and the fiddly bits —
numeric sorting, card chrome — are written once.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Card(QFrame):
    """A bordered panel. Everything on a screen sits in one of these."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")


class StatCard(Card):
    """A single headline number with a caption under it."""

    def __init__(self, label: str, value: str, *, tone: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(4)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        if tone:
            # Colour is set by object name so the palette stays in theme.py.
            self.value_label.setObjectName(tone)
            self.value_label.setStyleSheet("font-size: 28px; font-weight: 600;")

        caption = QLabel(label.upper())
        caption.setObjectName("StatLabel")

        layout.addWidget(self.value_label)
        layout.addWidget(caption)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class StatRow(QWidget):
    """A row of StatCards across the top of a screen."""

    def __init__(self, cards: list[StatCard]) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for card in cards:
            layout.addWidget(card)


class SortableItem(QTableWidgetItem):
    """A cell that sorts on a real value rather than its displayed text.

    Without this "$1,850.00" sorts before "$9.99" because Qt compares the
    strings, which makes every money column in the app quietly wrong.
    """

    def __init__(self, text: str, sort_key: float | int | str | tuple) -> None:
        super().__init__(text)
        self._sort_key = sort_key
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, SortableItem):
            try:
                return self._sort_key < other._sort_key
            except TypeError:
                # Mixed key types in one column should not crash a sort.
                return str(self._sort_key) < str(other._sort_key)
        return super().__lt__(other)


class FlowLayout(QLayout):
    """A layout that wraps its children onto as many rows as it needs.

    Qt's tab bars scroll horizontally when they run out of room, which puts
    little arrows in front of half the tabs. With ten accounts that is most of
    them. Wrapping shows everything at once instead, which is what a strip of
    filters wants to do.
    """

    def __init__(self, parent=None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        """Place items left to right, wrapping. Returns the height used."""
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        right = rect.right() - margins.right()
        x, y = left, rect.y() + margins.top()
        row_height = 0

        for item in self._items:
            hint = item.sizeHint()
            if x > left and x + hint.width() > right:
                x = left
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            row_height = max(row_height, hint.height())

        return y + row_height - rect.y() + margins.bottom()


class FilterStrip(QWidget):
    """A wrapping row of exclusive filter buttons.

    A drop-in replacement for the parts of QTabBar these screens actually use,
    minus the horizontal scrolling: every option stays visible at once, which
    is the point of a filter strip.
    """

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = FlowLayout(self, spacing=6)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        self._data: list[object] = []
        self._group.idClicked.connect(self.currentChanged.emit)

    def addTab(self, label: str) -> int:  # noqa: N802
        button = QPushButton(label)
        button.setObjectName("FilterChip")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        index = len(self._buttons)
        button.setChecked(index == 0)
        self._group.addButton(button, index)
        self._buttons.append(button)
        self._data.append(None)
        self._layout.addWidget(button)
        return index

    def removeTab(self, index: int) -> None:  # noqa: N802
        if not 0 <= index < len(self._buttons):
            return
        button = self._buttons.pop(index)
        self._data.pop(index)
        self._group.removeButton(button)
        self._layout.removeWidget(button)
        button.deleteLater()
        # Ids must stay equal to positions, or tabData looks up the wrong row.
        for position, remaining in enumerate(self._buttons):
            self._group.setId(remaining, position)

    def count(self) -> int:
        return len(self._buttons)

    def tabText(self, index: int) -> str:  # noqa: N802
        return self._buttons[index].text() if 0 <= index < len(self._buttons) else ""

    def setTabData(self, index: int, value: object) -> None:  # noqa: N802
        if 0 <= index < len(self._data):
            self._data[index] = value

    def tabData(self, index: int) -> object:  # noqa: N802
        return self._data[index] if 0 <= index < len(self._data) else None

    def setTabToolTip(self, index: int, text: str) -> None:  # noqa: N802
        if 0 <= index < len(self._buttons):
            self._buttons[index].setToolTip(text)

    def currentIndex(self) -> int:  # noqa: N802
        return max(self._group.checkedId(), 0)

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)
            self.currentChanged.emit(index)

    def blockSignals(self, block: bool) -> bool:  # noqa: N802
        self._group.blockSignals(block)
        return super().blockSignals(block)


class HoverRowDelegate(QStyledItemDelegate):
    """Paints the whole row under the cursor, not just the cell.

    Qt's `::item:hover` stylesheet rule applies per cell, so on a wide table
    only the one cell beneath the pointer changes and the eye still loses the
    line between a merchant and its amount. Tracking the row in a delegate is
    the supported way to highlight all of it.
    """

    def __init__(self, view) -> None:
        super().__init__(view)
        self._view = view
        self._row = -1
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.MouseMove:
            index = self._view.indexAt(event.position().toPoint())
            row = index.row() if index.isValid() else -1
            if row != self._row:
                self._row = row
                self._view.viewport().update()
        elif event.type() == QEvent.Type.Leave:
            if self._row != -1:
                self._row = -1
                self._view.viewport().update()
        return False

    def paint(self, painter, option, index) -> None:
        if index.row() == self._row and not (option.state & QStyle.StateFlag.State_Selected):
            # Under the text, so foreground colours set per cell survive.
            painter.fillRect(option.rect, QColor(theme.ACTIVE.hover))
        super().paint(painter, option, index)


def enable_row_hover(view) -> HoverRowDelegate:
    """Attach row highlighting to a table, keeping the delegate alive."""
    delegate = HoverRowDelegate(view)
    view.setItemDelegate(delegate)
    return delegate


class ColumnFilter(QObject):
    """Right-click a column header to show only some of its values.

    A table that already carries sorting still cannot answer "show me only the
    cancelled ones" — sorting groups them but leaves everything else on
    screen. This adds a per-column value filter, driven from the column's own
    contents so it never offers a value the table does not contain.

    Emits `changed` when the selection moves; the view decides what to hide,
    since only it knows which rows a value belongs to.
    """

    changed = Signal()

    def __init__(self, table) -> None:
        super().__init__(table)
        self._table = table
        # column -> the values allowed. A column absent from this map is
        # unfiltered, which keeps "no filter" distinct from "everything ticked
        # by hand" for the header marker.
        self.allowed: dict[int, set[str]] = {}

        header = table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._menu)

    def is_filtered(self, column: int) -> bool:
        return column in self.allowed

    def accepts(self, column: int, value: str) -> bool:
        allowed = self.allowed.get(column)
        return allowed is None or value in allowed

    def clear(self) -> None:
        if self.allowed:
            self.allowed.clear()
            self.changed.emit()

    def _values(self, column: int) -> list[str]:
        """Every distinct value in a column, whatever is currently hidden.

        Read from the model rather than the visible rows, or filtering to one
        value would leave no way back to the others.
        """
        seen = {
            self._table.item(row, column).text()
            for row in range(self._table.rowCount())
            if self._table.item(row, column)
        }
        return sorted(v for v in seen if v)

    def _menu(self, position) -> None:
        header = self._table.horizontalHeader()
        column = header.logicalIndexAt(position)
        if column < 0:
            return

        values = self._values(column)
        if not values:
            return

        menu = QMenu(self._table)
        label = self._table.horizontalHeaderItem(column)
        menu.addSection(f"Show only — {label.text() if label else ''}")

        allowed = self.allowed.get(column)
        for value in values:
            action = QAction(value, menu)
            action.setCheckable(True)
            action.setChecked(allowed is None or value in allowed)
            action.toggled.connect(lambda checked, c=column, v=value: self._toggle(c, v, checked))
            menu.addAction(action)

        menu.addSeparator()
        show_all = QAction("Show all", menu)
        show_all.setEnabled(column in self.allowed)
        show_all.triggered.connect(lambda: self._reset(column))
        menu.addAction(show_all)

        if self.allowed:
            everything = QAction("Clear every column filter", menu)
            everything.triggered.connect(self.clear)
            menu.addAction(everything)

        menu.exec(header.mapToGlobal(position))

    def _toggle(self, column: int, value: str, checked: bool) -> None:
        # An unfiltered column starts as everything, so unticking one value
        # means "all but this" rather than "only this".
        allowed = self.allowed.get(column)
        if allowed is None:
            allowed = set(self._values(column))
        allowed = set(allowed)

        if checked:
            allowed.add(value)
        else:
            allowed.discard(value)

        if not allowed:
            # Hiding everything leaves an empty table with no way back, so an
            # empty selection means no filter.
            self.allowed.pop(column, None)
        elif allowed == set(self._values(column)):
            self.allowed.pop(column, None)
        else:
            self.allowed[column] = allowed
        self.changed.emit()

    def _reset(self, column: int) -> None:
        if self.allowed.pop(column, None) is not None:
            self.changed.emit()
