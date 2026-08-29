"""Small shared widgets.

Kept separate so the screens stay mostly layout code and the fiddly bits —
numeric sorting, card chrome — are written once.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


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

    def __init__(self, text: str, sort_key: float | int | str) -> None:
        super().__init__(text)
        self._sort_key = sort_key
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, SortableItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)
