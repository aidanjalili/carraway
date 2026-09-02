"""Chart widgets for the spending view.

Drawn with QPainter rather than a charting library. Three simple chart types
for one series each is a few hundred lines, it follows the app's palette for
free, and it keeps the promise that Qt is the only runtime dependency.

The palette steps one hue through lightness rather than assigning a different
colour per category. Distinct hues would imply the categories are unrelated
things; they are all the same thing — money leaving — and the useful comparison
is size, which lightness ordering supports and a rainbow actively fights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ...core.money import Money
from .. import theme
from ..widgets import shorten


@dataclass(frozen=True, slots=True)
class Slice:
    """One category's share of a period."""

    label: str
    amount: Money
    fraction: float


# A categorical palette: distinct hues, deliberately not evenly spaced around
# the wheel. Even spacing puts adjacent categories at similar lightness, which
# is where colour-blind viewers lose the distinction; these are picked so no
# two neighbours share both hue family and lightness.
#
# Saturation and lightness are held in a narrow band so every slice reads at
# the same weight on both a dark and a light background — a fully saturated
# yellow beside a deep blue makes the yellow look like the important one.
_HUES: tuple[tuple[float, float, float], ...] = (
    (0.39, 0.52, 0.52),  # green
    (0.58, 0.55, 0.55),  # blue
    (0.08, 0.62, 0.56),  # amber
    (0.78, 0.45, 0.60),  # violet
    (0.02, 0.58, 0.58),  # coral
    (0.48, 0.48, 0.46),  # teal
    (0.92, 0.50, 0.62),  # pink
    (0.13, 0.45, 0.48),  # olive
    (0.66, 0.50, 0.62),  # periwinkle
    (0.05, 0.40, 0.45),  # rust
    (0.33, 0.42, 0.45),  # moss
    (0.85, 0.38, 0.52),  # plum
)


def _shade(index: int, count: int) -> QColor:
    """A colour for slice `index`.

    Cycles the palette, darkening each time round so a thirteenth category is
    distinguishable from the first rather than identical to it.
    """
    hue, saturation, lightness = _HUES[index % len(_HUES)]
    cycle = index // len(_HUES)
    if cycle:
        # Each lap is meaningfully darker; clamped so it never reaches black.
        lightness = max(0.22, lightness - 0.13 * cycle)
    return QColor.fromHslF(hue, saturation, lightness)


class _ChartBase(QWidget):
    """Shared hover handling: the hit-test differs, the tooltip does not."""

    sliceClicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.slices: list[Slice] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._hovered: int | None = None

    def set_slices(self, slices: list[Slice]) -> None:
        self.slices = slices
        self._hovered = None
        self.update()

    def _hit(self, position) -> int | None:
        raise NotImplementedError

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._hit(event.position())
        if index != self._hovered:
            self._hovered = index
            self.update()
        if index is not None:
            item = self.slices[index]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{item.label}\n{item.amount.format()}  ({item.fraction:.1%})",
                self,
            )
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = None
        QToolTip.hideText()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        index = self._hit(event.position())
        if index is not None:
            self.sliceClicked.emit(self.slices[index].label)

    def _empty_message(self, painter: QPainter) -> None:
        painter.setPen(QColor(theme.ACTIVE.muted))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nothing spent in this period")


class PieChart(_ChartBase):
    """A donut of category shares, with a legend beside it.

    The legend is not optional: a ring of slices with no labels can only be
    read by hovering every one, which is not reading a chart.
    """

    # Enough room for a category name and its share without crowding the ring.
    LEGEND_WIDTH = 210

    def __init__(self) -> None:
        super().__init__()
        self._geometry: tuple[QPointF, float, float] = (QPointF(), 0.0, 0.0)
        self._legend_rows: list[QRectF] = []

    def _hit(self, position) -> int | None:
        for index, row in enumerate(self._legend_rows):
            if row.contains(position):
                return index
        centre, outer, inner = self._geometry
        if outer <= 0:
            return None
        offset = position - centre
        distance = math.hypot(offset.x(), offset.y())
        if not inner <= distance <= outer:
            return None
        # Qt angles run anticlockwise from 3 o'clock; screen y grows downward.
        angle = math.degrees(math.atan2(-offset.y(), offset.x())) % 360
        start = 90.0
        for index, item in enumerate(self.slices):
            sweep = item.fraction * 360
            if (start - angle) % 360 <= sweep:
                return index
            start -= sweep
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.slices:
            self._empty_message(painter)
            return

        # The ring takes the space left over once the legend has its column,
        # so neither is squeezed by the other.
        legend_width = self.LEGEND_WIDTH if self.width() > 520 else 0
        ring_width = self.width() - legend_width
        side = min(ring_width, self.height()) - 24
        centre = QPointF(ring_width / 2, self.height() / 2)
        outer = side / 2
        inner = outer * 0.58
        self._geometry = (centre, outer, inner)

        box = QRectF(centre.x() - outer, centre.y() - outer, side, side)
        start = 90.0
        for index, item in enumerate(self.slices):
            sweep = item.fraction * 360
            colour = _shade(index, len(self.slices))
            if index == self._hovered:
                colour = colour.lighter(125)
            painter.setBrush(colour)
            painter.setPen(QPen(QColor(theme.ACTIVE.surface), 2))
            # Qt measures angles in sixteenths of a degree.
            painter.drawPie(box, int(start * 16), int(-sweep * 16))
            start -= sweep

        # Punch the hole after the slices, so the total can sit inside it.
        hole = QPainterPath()
        hole.addEllipse(centre, inner, inner)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACTIVE.surface))
        painter.drawPath(hole)

        total = self.slices[0].amount
        for item in self.slices[1:]:
            total = total + item.amount
        painter.setPen(QColor(theme.ACTIVE.text))
        font = QFont(painter.font())
        font.setPointSize(15)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(centre.x() - inner, centre.y() - 14, inner * 2, 28),
            Qt.AlignmentFlag.AlignCenter,
            total.format(),
        )

        self._legend_rows = []
        if not legend_width:
            return

        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        row_height = 22
        left = self.width() - legend_width + 6
        # Centred vertically against the ring, and truncated rather than
        # overflowing when a period has more categories than there is room for.
        visible = min(len(self.slices), max(1, int(self.height() / row_height) - 1))
        top = max(8.0, (self.height() - visible * row_height) / 2)

        for index, item in enumerate(self.slices[:visible]):
            row_top = top + index * row_height
            self._legend_rows.append(QRectF(left, row_top, legend_width - 12, row_height))

            colour = _shade(index, len(self.slices))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(left, row_top + 6, 10, 10), 2, 2)

            painter.setPen(
                QColor(theme.ACTIVE.text if index == self._hovered else theme.ACTIVE.muted)
            )
            painter.drawText(
                QRectF(left + 18, row_top, legend_width - 90, row_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                shorten(item.label, 20),
            )
            painter.drawText(
                QRectF(left + legend_width - 84, row_top, 72, row_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{item.fraction:.0%}",
            )

        hidden = len(self.slices) - visible
        if hidden > 0:
            painter.setPen(QColor(theme.ACTIVE.muted))
            painter.drawText(
                QRectF(left + 18, top + visible * row_height, legend_width - 24, row_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+{hidden} more",
            )


class BarChart(_ChartBase):
    """Horizontal bars, which compare lengths far better than angles."""

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[QRectF] = []

    def _hit(self, position) -> int | None:
        for index, rect in enumerate(self._rows):
            if rect.contains(position):
                return index
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.slices:
            self._empty_message(painter)
            return

        palette = theme.ACTIVE
        margin = 8
        label_width = 132
        value_width = 96
        row_height = min(30, max(18, (self.height() - margin * 2) // len(self.slices)))
        track_left = margin + label_width
        track_width = max(40, self.width() - margin * 2 - label_width - value_width)
        largest = max(item.amount.minor for item in self.slices) or 1

        self._rows = []
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)

        for index, item in enumerate(self.slices):
            top = margin + index * row_height
            if top + row_height > self.height():
                break
            bar_height = row_height - 8
            width = track_width * item.amount.minor / largest
            rect = QRectF(track_left, top + 4, max(width, 2), bar_height)
            self._rows.append(QRectF(margin, top, self.width() - margin * 2, row_height))

            colour = _shade(index, len(self.slices))
            if index == self._hovered:
                colour = colour.lighter(125)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(rect, 3, 3)

            painter.setPen(QColor(palette.text))
            painter.drawText(
                QRectF(margin, top, label_width - 8, row_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                shorten(item.label, 20),
            )
            painter.setPen(QColor(palette.muted))
            painter.drawText(
                QRectF(self.width() - margin - value_width, top, value_width, row_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                item.amount.format(),
            )


class TrendChart(_ChartBase):
    """Spending per period as columns, for looking across time rather than within one."""

    def __init__(self) -> None:
        super().__init__()
        self._columns: list[QRectF] = []

    def _hit(self, position) -> int | None:
        for index, rect in enumerate(self._columns):
            if rect.contains(position):
                return index
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.slices:
            self._empty_message(painter)
            return

        palette = theme.ACTIVE
        margin = 10
        label_band = 20
        width = self.width() - margin * 2
        height = self.height() - margin * 2 - label_band
        largest = max(item.amount.minor for item in self.slices) or 1
        gap = 3
        column_width = max(2.0, width / len(self.slices) - gap)

        self._columns = []
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)

        for index, item in enumerate(self.slices):
            left = margin + index * (column_width + gap)
            column_height = height * item.amount.minor / largest
            rect = QRectF(left, margin + height - column_height, column_width, column_height)
            self._columns.append(QRectF(left, margin, column_width, height))

            # One series across time, so one colour: varying it by period
            # would imply the periods are different kinds of thing.
            colour = QColor(palette.accent)
            if index == self._hovered:
                colour = colour.lighter(130)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(rect, 2, 2)

        # Only the ends are labelled: a hundred weekly ticks is unreadable, and
        # hovering gives the exact period anyway.
        painter.setPen(QColor(palette.muted))
        baseline = QRectF(margin, self.height() - label_band, width, label_band)
        painter.drawText(baseline, Qt.AlignmentFlag.AlignLeft, self.slices[0].label)
        painter.drawText(baseline, Qt.AlignmentFlag.AlignRight, self.slices[-1].label)
