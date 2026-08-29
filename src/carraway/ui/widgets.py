"""Small shared widgets.

Kept separate so the screens stay mostly layout code and the fiddly bits —
numeric sorting, card chrome — are written once.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
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
