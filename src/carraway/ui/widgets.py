"""Small shared widgets.

Kept separate so the screens stay mostly layout code and the fiddly bits —
numeric sorting, card chrome — are written once.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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


class InfoDot(QPushButton):
    """A small "i" beside a control, explaining what that control does.

    A tooltip would have been less code, but a tooltip is only found by
    hovering something you already wondered about — which is the wrong way
    round, since the person who needs the explanation is the one who does not
    yet know there is a question. A visible dot advertises that there is
    something to read.

    The text appears in a popup rather than a tooltip so it can be several
    lines long and stays put while it is read.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__("i", parent)
        self.setObjectName("InfoDot")
        self.explanation = text
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(17, 17)
        self.setFlat(True)
        # Focusing it would put it in the tab order between a control and its
        # own input, which is not where anyone is trying to get to.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(text)
        self.clicked.connect(self.explain)

    def setExplanation(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self.explanation = text
        self.setToolTip(text)

    def explain(self) -> None:
        """Show the popup under the dot."""
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("InfoPopup")
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(self.explanation)
        label.setWordWrap(True)
        label.setObjectName("Muted")
        # Wide enough to read a sentence without becoming a paragraph-shaped
        # column, and the popup grows downwards from there.
        label.setMinimumWidth(300)
        label.setMaximumWidth(340)
        layout.addWidget(label)
        popup.adjustSize()
        popup.move(self.mapToGlobal(QPoint(0, self.height() + 4)))
        popup.show()


def labelled(text: str, explanation: str, *, heading: bool = False) -> QWidget:
    """A label with an info dot after it, as one widget a layout can hold."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    label = QLabel(text)
    if heading:
        label.setObjectName("SectionHeading")
    row.addWidget(label)
    row.addWidget(InfoDot(explanation))
    row.addStretch(1)
    return holder


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

    def set_comparison(self, text: str, tone: str = "Muted") -> None:
        """A line under the caption saying how this moved. Empty text hides it.

        `tone` is an object name -- "Accent", "Danger" or "Muted" -- so the
        colours stay in theme.py rather than being spelled out at each call
        site, the same way the headline value does it.

        Added lazily rather than in __init__ so every StatCard on every other
        screen keeps exactly the height it has today.
        """
        if not hasattr(self, "_comparison"):
            self._comparison = QLabel("")
            self.layout().addWidget(self._comparison)
        self._comparison.setText(text)
        self._comparison.setVisible(bool(text))
        self._comparison.setObjectName(tone or "Muted")
        self._comparison.setStyleSheet("font-size: 11px;")
        # An object name changed after styling needs the polish redone, or the
        # new selector is not applied until something else forces a repaint.
        self._comparison.style().unpolish(self._comparison)
        self._comparison.style().polish(self._comparison)


class BalanceBanner(Card):
    """The headline balance for whatever the screen is currently showing.

    Sits directly above a table and outside its scroll area, so it stays put
    while the rows move — the number you want while reading a statement is
    the one you are reading it against, and it should not scroll away.

    Colour carries the sign so the direction reads before the digits do:
    green for money you have, red for money you owe. That is the same
    question in both cases, asked of an asset and of a liability, so one
    control answers both rather than the reader having to remember which kind
    of account this tab is.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 10, 18, 11)
        row.setSpacing(12)

        figure = QVBoxLayout()
        figure.setSpacing(1)
        self.amount = QLabel("")
        self.amount.setObjectName("BalanceValue")
        self.caption = QLabel("")
        self.caption.setObjectName("StatLabel")
        figure.addWidget(self.amount)
        figure.addWidget(self.caption)
        row.addLayout(figure)
        row.addStretch(1)

        # Buttons that act on whatever the banner is describing. Empty on most
        # screens; a cash account puts its "set balance" here, beside the
        # number it changes rather than in a menu somewhere else.
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)

    def add_action(self, button: QWidget) -> None:
        """Attach a button beside the figure."""
        self.actions.addWidget(button)

    def show_balance(self, amount: str, caption: str, *, owed: bool) -> None:
        """Set the figure and its caption. `owed` picks the colour."""
        from . import theme

        tone = theme.ACTIVE.danger if owed else theme.ACTIVE.accent
        self.amount.setText(amount)
        self.amount.setStyleSheet(f"font-size: 30px; font-weight: 700; color: {tone};")
        self.caption.setText(caption.upper())
        self.setVisible(True)

    def show_nothing(self, caption: str) -> None:
        """No figure to show — say so plainly rather than showing a zero.

        A zero and an unknown look identical and mean opposite things, and
        this app has accounts with no recorded balance at all.
        """
        from . import theme

        self.amount.setText("—")
        self.amount.setStyleSheet(
            f"font-size: 30px; font-weight: 700; color: {theme.ACTIVE.muted};"
        )
        self.caption.setText(caption.upper())
        self.setVisible(True)


class QRCode(QWidget):
    """A QR code drawn at whatever size it is given.

    Always black on white, whatever the theme is doing. A phone camera is
    looking for a dark-on-light pattern, and a code rendered in the dark
    palette's foreground on its background is a code that does not scan --
    which is the kind of bug you find standing in a shop, so it is worth
    the small inconsistency here.
    """

    # Four modules of clear space on every side, which the spec requires and
    # decoders genuinely rely on to find the code at all.
    QUIET = 4

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid: list[list[int]] = []
        self.setText(text)

    def setText(self, text: str) -> None:
        from .qr import encode

        self._grid = encode(text) if text else []
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(240, 240)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 160)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if not self._grid:
            return
        modules = len(self._grid) + self.QUIET * 2
        side = min(self.width(), self.height())

        # Whole pixels per module, or the resampling blurs module edges into
        # each other and a marginal camera stops reading it.
        scale = max(1, side // modules)
        drawn = scale * modules
        left = (self.width() - drawn) // 2
        top = (self.height() - drawn) // 2

        painter = QPainter(self)
        painter.fillRect(left, top, drawn, drawn, QColor("#ffffff"))
        painter.setBrush(QColor("#000000"))
        painter.setPen(Qt.PenStyle.NoPen)
        for row, cells in enumerate(self._grid):
            for column, cell in enumerate(cells):
                if cell:
                    painter.drawRect(
                        left + (column + self.QUIET) * scale,
                        top + (row + self.QUIET) * scale,
                        scale,
                        scale,
                    )
        painter.end()

    def pixmap(self, scale: int = 8) -> QPixmap:
        """The same code as an image, for saving or copying."""
        modules = len(self._grid) + self.QUIET * 2
        out = QPixmap(modules * scale, modules * scale)
        out.fill(QColor("#ffffff"))
        painter = QPainter(out)
        painter.setBrush(QColor("#000000"))
        painter.setPen(Qt.PenStyle.NoPen)
        for row, cells in enumerate(self._grid):
            for column, cell in enumerate(cells):
                if cell:
                    painter.drawRect(
                        (column + self.QUIET) * scale,
                        (row + self.QUIET) * scale,
                        scale,
                        scale,
                    )
        painter.end()
        return out


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
    # The labels, in their new order, after the user drags one somewhere else.
    orderChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = FlowLayout(self, spacing=6)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        self._data: list[object] = []
        self._group.idClicked.connect(self.currentChanged.emit)

        # Drag-to-reorder state. `_press` is where the mouse went down, kept
        # so a drag only starts once it has moved far enough to be meant --
        # otherwise every click on a chip would jitter the strip.
        self._press: QPoint | None = None
        self._dragging = -1
        self._moved = False
        self.reorderable = False

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
        button.installEventFilter(self)
        return index

    # -- dragging one chip somewhere else --------------------------------

    def setReorderable(self, enabled: bool) -> None:  # noqa: N802
        """Allow the user to drag chips into a different order."""
        self.reorderable = enabled
        for button in self._buttons:
            button.setToolTip("Drag to reorder" if enabled else button.toolTip())

    def labels(self) -> list[str]:
        return [button.text() for button in self._buttons]

    def moveTab(self, source: int, target: int) -> None:  # noqa: N802
        """Move one chip to another position, keeping data and selection."""
        if source == target:
            return
        if not (0 <= source < len(self._buttons) and 0 <= target < len(self._buttons)):
            return
        self._buttons.insert(target, self._buttons.pop(source))
        self._data.insert(target, self._data.pop(source))

        # FlowLayout has no insert, so the row is emptied and refilled. The
        # buttons keep their parent throughout, so nothing is destroyed.
        while self._layout.count():
            self._layout.takeAt(0)
        for button in self._buttons:
            self._layout.addWidget(button)

        # Ids must equal positions, or tabData and currentIndex disagree.
        for position, button in enumerate(self._buttons):
            self._group.setId(button, position)
        self._layout.invalidate()
        self.updateGeometry()

    def _chip_under(self, global_pos) -> int:
        for index, button in enumerate(self._buttons):
            local = button.mapFromGlobal(global_pos)
            if button.rect().contains(local):
                return index
        return -1

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if not self.reorderable or watched not in self._buttons:
            return super().eventFilter(watched, event)

        if event.type() == QEvent.Type.MouseButtonPress:
            self._press = event.globalPosition().toPoint()
            self._dragging = self._buttons.index(watched)
            self._moved = False
        elif event.type() == QEvent.Type.MouseMove and self._press is not None:
            here = event.globalPosition().toPoint()
            if (here - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._moved = True
                target = self._chip_under(here)
                if target >= 0 and target != self._dragging:
                    self.moveTab(self._dragging, target)
                    self._dragging = target
        elif event.type() == QEvent.Type.MouseButtonRelease:
            dragged = self._moved
            self._press = None
            self._dragging = -1
            self._moved = False
            if dragged:
                # Swallow the release so the drag does not also count as a
                # click, which would switch tabs to wherever it was dropped.
                self.orderChanged.emit(self.labels())
                return True
        return super().eventFilter(watched, event)

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


class MeterDelegate(HoverRowDelegate):
    """Draws a filled bar for the cell's value, keeping the row hover intact.

    Subclasses the hover delegate rather than replacing it: a column with its
    own delegate would otherwise be the one column that does not light up with
    the rest of its row, which reads as a rendering bug.

    The value is a fraction in `Qt.UserRole`; above 1.0 the bar is full and
    turns red, because "180% of budget" has no longer bar to draw and the
    colour is the part that matters anyway.
    """

    def paint(self, painter, option, index) -> None:
        fraction = index.data(Qt.ItemDataRole.UserRole)
        if fraction is None:
            super().paint(painter, option, index)
            return

        # The hover wash, without the text: the bar is the content here.
        if index.row() == self._row and not (option.state & QStyle.StateFlag.State_Selected):
            painter.fillRect(option.rect, QColor(theme.ACTIVE.hover))

        rect = option.rect.adjusted(6, 0, -6, 0)
        height = 8
        top = rect.top() + (rect.height() - height) // 2
        track = QRect(rect.left(), top, rect.width(), height)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACTIVE.surface_alt))
        painter.drawRoundedRect(track, 4, 4)

        filled = max(0.0, min(float(fraction), 1.0))
        if filled > 0:
            over = float(fraction) > 1.0
            painter.setBrush(QColor(theme.ACTIVE.danger if over else theme.ACTIVE.accent))
            width = max(int(track.width() * filled), 3)
            painter.drawRoundedRect(QRect(track.left(), top, width, height), 4, 4)
        painter.restore()


def enable_row_hover(view) -> HoverRowDelegate:
    """Attach row highlighting to a table, keeping the delegate alive."""
    delegate = HoverRowDelegate(view)
    view.setItemDelegate(delegate)
    return delegate


def refresh_everything(widget: QWidget) -> None:
    """Rebuild every screen, not just the one the user is looking at.

    A classification is a fact about the ledger rather than about one table.
    Hiding a series in Subscriptions has to remove it from Upcoming as well,
    and Upcoming is by definition not the tab in front of the user when it
    happens — so refreshing only the active view leaves the other tables
    showing something the ledger no longer contains, until the app restarts.

    Falls back to refreshing just this widget when there is no window to ask,
    which is how the screens behave in tests.
    """
    refresh_all = getattr(widget.window(), "refresh_all", None)
    if callable(refresh_all):
        refresh_all()
        return
    own = getattr(widget, "refresh", None)
    if callable(own):
        own()
