"""Small shared widgets.

Kept separate so the screens stay mostly layout code and the fiddly bits —
numeric sorting, card chrome — are written once.
"""

from __future__ import annotations

import contextlib

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme


def as_tooltip(text: str, width: int = 380) -> str:
    """Turn explanatory text into a tooltip that wraps instead of running off.

    Qt lays a plain-text tooltip out on one line per paragraph and never
    wraps it, so a two-sentence explanation becomes a strip wider than the
    screen with its end unreadable. A *rich text* tooltip wraps, so the text
    is escaped, blank lines become paragraph breaks, and the whole thing is
    given a width to wrap inside.
    """
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    paragraphs = [p.strip() for p in escaped.split("\n\n") if p.strip()]
    body = "<br><br>".join(par.replace("\n", " ") for par in paragraphs)
    return f'<qt><div style="width: {width}px">{body}</div></qt>'


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

    def set_explanation(self, text: str) -> None:
        """Change what this dot says, popup and tooltip together.

        Not `setText`, which would replace the "i" with the paragraph, and
        not the attribute on its own, which leaves the tooltip saying
        something the popup no longer does.
        """
        self.explanation = text
        self.setToolTip(as_tooltip(text))

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
        self.setToolTip(as_tooltip(text))
        self.clicked.connect(self.explain)

    def setExplanation(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self.explanation = text
        self.setToolTip(as_tooltip(text))

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


def shorten(text: str, limit: int) -> str:
    """Trim to `limit`, with an ellipsis so the reader knows it was trimmed.

    Slicing alone produced "Alpaca Brokerage (0388" and "EPIC SYSTEMS
    CORPORATI" -- names that look like data errors rather than labels that
    ran out of room. A word boundary is preferred when there is one close to
    the end, because cutting mid-word is what made them read as broken.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit - 8:
        cut = cut[:space].rstrip()
    return f"{cut}…"


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
    # Every checked index, when multi-select is on. Emitted instead of
    # currentChanged, because "which one" stops being a question that has an
    # answer the moment two can be on at once.
    selectionChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = FlowLayout(self, spacing=6)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        self._data: list[object] = []
        self._multi = False
        self._group.idClicked.connect(self._chip_clicked)

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

    # -- one at a time, or several ---------------------------------------

    def setMultiSelect(self, enabled: bool) -> None:  # noqa: N802
        """Let several chips be on at once.

        Off by default, so every strip that has only ever meant "one of
        these" keeps behaving exactly as it did. Turning it on drops the
        button group's exclusivity, which is also what makes a checked chip
        clickable *off* -- an exclusive group refuses to uncheck its last
        checked button, by design.
        """
        self._multi = enabled
        self._group.setExclusive(not enabled)

    def selectedIndexes(self) -> list[int]:  # noqa: N802
        return [i for i, button in enumerate(self._buttons) if button.isChecked()]

    def setSelectedIndexes(self, indexes) -> None:  # noqa: N802
        """Check exactly these and nothing else. Emits nothing."""
        wanted = set(indexes)
        exclusive = self._group.exclusive()
        # Lifted for the duration: an exclusive group will not let the last
        # checked button go, so clearing one to set another would silently
        # leave both on.
        self._group.setExclusive(False)
        for index, button in enumerate(self._buttons):
            button.setChecked(index in wanted)
        self._group.setExclusive(exclusive)

    def _chip_clicked(self, index: int) -> None:
        if self._multi:
            self.selectionChanged.emit(self.selectedIndexes())
        else:
            self.currentChanged.emit(index)

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
        # Not `return False`. This method is installed on the viewport for the
        # hover wash, but it is also the one QStyledItemDelegate uses on the
        # *editor* -- it is what commits a cell on Return and abandons it on
        # Escape. Swallowing everything this class does not care about meant
        # pressing Enter in a cell did nothing at all, and the only way to save
        # a typed figure was to click somewhere else and let focus-out do it.
        return super().eventFilter(watched, event)

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


def dress_calendar(picker) -> None:
    """Make a QDateEdit's popup calendar readable, and say which day is today.

    Seven date pickers across the app were each setting some of this and none
    of them all of it, so the popup looked slightly different depending on
    which dialog opened it.

    Marking today matters more than it sounds. The calendar opens on whatever
    date the field holds, which is often months away from now, and with no
    "you are here" the only way to tell where today sits is to read the
    month title and do the arithmetic. That is exactly how a year gets picked
    wrong by one.

    The mark is set once, when the picker is built. A dialog is short-lived so
    that is always current; the long-lived pickers on Transactions would go a
    day stale if the app were left open across midnight, which is a cosmetic
    edge on a screen that reloads when the date changes anyway.
    """
    from PySide6.QtCore import QDate
    from PySide6.QtGui import QTextCharFormat

    calendar = picker.calendarWidget()
    if calendar is None:
        return
    # The grid makes days easier to hit; the navigation bar is where the month
    # menu and the year spinner live -- the two controls that turn "next
    # month" into "pick any date".
    calendar.setGridVisible(True)
    calendar.setNavigationBarVisible(True)

    today = QDate.currentDate()
    mark = QTextCharFormat()
    mark.setFontWeight(QFont.Weight.Bold)
    mark.setForeground(QColor(theme.ACTIVE.accent))
    # A filled cell rather than only coloured text, so it still reads as
    # today at a glance on a grid of thirty other numbers.
    mark.setBackground(QColor(theme.ACTIVE.surface_alt))
    calendar.setDateTextFormat(today, mark)


class PanelSplitter(QSplitter):
    """Stacked panels the user can resize, maximise, and have remembered.

    Every screen that stacks two or three things has the same problem: the
    right split depends on the question being asked, not on what looked good
    when the screen was written. A chart deserves the room while you are
    reading a shape and almost none while you are reading figures off the
    table beneath it.

    So: drag the divider, or press a panel's button to give it everything.
    Both are saved per screen under `panels:<name>`, which is also what
    `reset_all` clears.

    A button rather than a double-click, because most of these panels are
    tables whose rows already answer to one -- and in at least one case
    double-clicking a row opens an editor. The gesture was taken before this
    wanted it.
    """

    #: Cleared together by Settings, so one control puts every screen back.
    PREFIX = "panels:"

    def __init__(self, name: str, ledger, defaults=None, parent=None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self._name = name
        self._ledger = ledger
        self._defaults = list(defaults or ())
        self._buttons: dict[int, QPushButton] = {}
        self._before_full: list[int] | None = None
        self._restoring = False
        self.setChildrenCollapsible(True)
        self.setHandleWidth(8)
        self.splitterMoved.connect(lambda *_: self.save())

    @property
    def setting_key(self) -> str:
        return f"{self.PREFIX}{self._name}"

    def add_panel(self, title: str, body: QWidget) -> QWidget:
        """Wrap `body` in a card with a thin header, and add it."""
        index = self.count()
        card = Card()
        inner = QVBoxLayout(card)
        inner.setContentsMargins(10, 10, 10, 10)
        inner.setSpacing(6)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 0, 0)
        label = QLabel(title)
        label.setObjectName("StatLabel")
        bar.addWidget(label)
        bar.addStretch(1)

        button = QPushButton("⤢")
        button.setObjectName("InfoDot")
        button.setFlat(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(22, 22)
        button.setToolTip(f"Give {title.lower()} the whole screen")
        button.clicked.connect(lambda _=False, i=index: self.toggle_full(i))
        bar.addWidget(button)
        self._buttons[index] = button

        inner.addLayout(bar)
        inner.addWidget(body)
        self.addWidget(card)
        # Stretch as well as sizes. `setSizes` is advisory until the splitter
        # has actually been laid out, so on a screen built before it is shown
        # the panels would come up sized by their content hints instead --
        # which is how a 5:3:3 default arrived on screen as very nearly 1:1:1.
        # Stretch factors survive that.
        self.setStretchFactor(index, self._defaults[index] if index < len(self._defaults) else 1)
        return card

    def toggle_full(self, index: int) -> None:
        sizes = self.sizes()
        others = [n for n in range(len(sizes)) if n != index]
        if others and all(sizes[n] == 0 for n in others):
            self.setSizes(self._before_full or self._sensible_defaults())
            self._before_full = None
        else:
            # Kept before collapsing, so leaving full screen returns to the
            # arrangement the user had rather than to the shipped default.
            self._before_full = sizes
            total = sum(sizes) or sum(self._sensible_defaults())
            self.setSizes([total if n == index else 0 for n in range(len(sizes))])
        self._sync()
        self.save()

    def _sensible_defaults(self) -> list[int]:
        if self._defaults and len(self._defaults) == self.count():
            return list(self._defaults)
        return [1] * max(1, self.count())

    def _sync(self) -> None:
        sizes = self.sizes()
        for index, button in self._buttons.items():
            others = [n for n in range(len(sizes)) if n != index]
            full = bool(others) and all(sizes[n] == 0 for n in others)
            button.setText("⤡" if full else "⤢")
            button.setToolTip("Back to the other panels" if full else button.toolTip())

    def save(self) -> None:
        if self._restoring:
            return
        state = self.saveState().toBase64().data().decode("ascii")
        self._ledger.save_setting(self.setting_key, state)

    def restore(self) -> None:
        """Put back what was saved, or fall back to the shipped proportions."""
        from PySide6.QtCore import QByteArray

        saved = self._ledger.setting(self.setting_key)
        self._restoring = True
        try:
            restored = False
            if isinstance(saved, str) and saved:
                restored = self.restoreState(QByteArray.fromBase64(saved.encode("ascii")))
            if not restored:
                self.setSizes(self._sensible_defaults())
        finally:
            self._restoring = False
        self._sync()

    def reset(self) -> None:
        self._before_full = None
        self.setSizes(self._sensible_defaults())
        self._sync()
        self.save()

    @classmethod
    def reset_all(cls, ledger) -> int:
        """Forget every screen's layout. Returns how many were cleared.

        Cleared rather than deleted: the settings table is a key/value store
        the app reads with `.setting()`, and an empty string already means
        "nothing saved" to `restore`. Adding a delete path for one caller
        would be a second way to say the same thing.
        """
        keys = [k for k in ledger.settings if str(k).startswith(cls.PREFIX)]
        for key in keys:
            ledger.save_setting(key, "")
        return len(keys)


#: Saved column widths live under this prefix, and are cleared with the panel
#: sizes by the one control in Settings.
#:
#: Version 2. The first attempt saved the header's whole state, which carries
#: resize *modes* as well as widths -- so layouts written while one column was
#: still Stretch came back with that column swallowing the table and every
#: figure beside it truncated. Those are not worth migrating; a new prefix
#: ignores them and starts from a sensible default.
COLUMN_PREFIX = "columns2:"


class _ColumnFit(QObject):
    """Keeps a table's columns filling its width, the way desktop apps do.

    Spare width is shared out in proportion to what each column already has,
    rather than dumped on one. Giving it all to a single column is right for
    a table with an obviously dominant text field and absurd for one made of
    five short figures -- where it produced a date column 1,291 pixels wide
    beside four squeezed to 81.

    Qt has no resize mode that does this: `Stretch` fills but cannot be
    dragged, `Interactive` drags but never fills.
    """

    #: Fallback floor, for columns never measured against real rows.
    FLOOR = 70

    #: No column's floor goes above this, however long its content is. A
    #: merchant column measured against "ALI JALILI MY MEET MOBILE
    #: SUBSCRIPTION..." wants six hundred pixels, and treating that as a
    #: minimum stops the table ever fitting a narrower window -- it overflowed
    #: by four hundred pixels rather than letting the text ellipsise, which is
    #: what a text column is supposed to do.
    FLOOR_CAP = 240

    def __init__(self, table, main: int, *, sized: bool = False) -> None:
        super().__init__(table)
        self._table = table
        self._main = main
        self._busy = False
        # False until the columns have been measured against real rows. A
        # view builds its table before it has anything in it, so sizing to
        # contents during __init__ measures an empty table -- which is how
        # every money column arrived 50 pixels wide with the figures cut off
        # and the date column swallowing the rest.
        self._sized = sized
        # Per column, the width its own content needs. A single global floor
        # cannot know that a date column needs ninety pixels and a category
        # needs sixty, so sharing out spare width would quietly squeeze one
        # of them until it truncated.
        self._floors: dict[int, int] = {}
        table.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self.fit()
        return super().eventFilter(watched, event)

    def columns(self) -> int:
        table = self._table
        if hasattr(table, "columnCount"):
            return table.columnCount()
        model = table.model()
        return model.columnCount() if model is not None else 0

    def rows(self) -> int:
        table = self._table
        if hasattr(table, "rowCount"):
            return table.rowCount()
        model = table.model()
        return model.rowCount() if model is not None else 0

    def size_to_contents(self) -> None:
        """Measure the columns against the rows now present, once."""
        from PySide6.QtWidgets import QHeaderView

        header = self._table.horizontalHeader()
        count = self.columns()
        self._busy = True
        try:
            for column in range(count):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            widths = [header.sectionSize(c) for c in range(count)]
            for column in range(count):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                # A little air either side of the content, and never so narrow
                # that a heading is unreadable.
                width = max(widths[column] + 22, 80)
                self._floors[column] = min(width, self.FLOOR_CAP)
                header.resizeSection(column, width)
        finally:
            self._busy = False
        self._sized = True

    def fit(self) -> None:
        if self._busy:
            return
        header = self._table.horizontalHeader()
        count = self.columns()
        if not (0 <= self._main < count):
            return
        if not self._sized and self.rows():
            self.size_to_contents()
        spare = self._table.viewport().width()
        if spare <= 0:
            return
        live = [c for c in range(count) if not self._table.isColumnHidden(c)]
        if not live:
            return
        sizes = {c: header.sectionSize(c) for c in live}
        used = sum(sizes.values())
        # A pixel or two either way is not worth a relayout, and chasing it
        # can oscillate against the scroll bar appearing and disappearing.
        if used <= 0 or abs(used - spare) <= 2:
            return

        share = spare / used
        self._busy = True
        try:
            widths = {
                c: max(self._floors.get(c, self.FLOOR), int(sizes[c] * share)) for c in live
            }
            # Rounding leaves a few pixels over; they go to the widest column,
            # where nobody will notice them.
            drift = spare - sum(widths.values())
            widest = max(live, key=lambda c: widths[c])
            widths[widest] = max(self._floors.get(widest, self.FLOOR), widths[widest] + drift)
            for column, width in widths.items():
                header.resizeSection(column, width)
        finally:
            self._busy = False


def resizable_columns(table, ledger, name: str, *, stretch: int = 0) -> None:
    """Size a table's columns sensibly, let them be dragged, remember them.

    What a table is expected to do, and what took three attempts to get
    right: start at widths that fit the content, let any divider be dragged,
    fill the width without a dead strip on the right, and come back tomorrow
    the way it was left.

    Qt gives none of that in one resize mode. `Stretch` fills but cannot be
    dragged -- and it was on the widest column, which is the first one anyone
    reaches for. `ResizeToContents` cannot be dragged either. `Interactive`
    can be dragged but never fills, which leaves either a gap or a scroll
    bar. So every column is Interactive and `_ColumnFit` does the filling.
    """
    from PySide6.QtCore import QByteArray
    from PySide6.QtWidgets import QHeaderView

    header = table.horizontalHeader()
    key = f"{COLUMN_PREFIX}{name}"

    if hasattr(table, "columnCount"):
        columns = table.columnCount()
    else:
        model = table.model()
        columns = model.columnCount() if model is not None else 0
    if not columns:
        return

    for column in range(columns):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

    saved = ledger.setting(key)
    restored = False
    if isinstance(saved, str) and saved:
        with contextlib.suppress(Exception):
            restored = header.restoreState(QByteArray.fromBase64(saved.encode("ascii")))
            # restoreState carries modes as well as widths, so they are put
            # back afterwards: the widths are the part worth keeping.
            for column in range(columns):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

    # Sized against real rows the first time there are any; a remembered
    # layout is already the right answer and is left alone.
    table._carraway_fit = _ColumnFit(table, stretch, sized=bool(restored))

    def remember(*_args) -> None:
        ledger.save_setting(key, header.saveState().toBase64().data().decode("ascii"))

    # Debounced: sectionResized fires for every pixel of a drag, and each one
    # would otherwise be a database write.
    header.sectionResized.connect(lambda *_: _debounce(header, remember))
    header.sectionMoved.connect(remember)


def _debounce(owner, fn, delay: int = 400) -> None:
    """Run `fn` once the caller has stopped firing for `delay` ms.

    One timer per owner, holding the latest callback rather than a growing
    list of connections. Qt warns when you disconnect a signal that has
    nothing attached -- and a warning is not an exception, so suppressing
    exceptions around it does nothing -- hence the timer carries its own
    callback and is connected exactly once.
    """
    from PySide6.QtCore import QTimer

    timer = getattr(owner, "_carraway_debounce", None)
    if timer is None:
        timer = QTimer(owner)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: getattr(timer, "_carraway_fn", lambda: None)())
        owner._carraway_debounce = timer
    timer._carraway_fn = fn
    timer.start(delay)


def reset_columns(ledger) -> int:
    """Forget every remembered column width. Returns how many were cleared."""
    keys = [k for k in ledger.settings if str(k).startswith(COLUMN_PREFIX)]
    for key in keys:
        ledger.save_setting(key, "")
    return len(keys)
