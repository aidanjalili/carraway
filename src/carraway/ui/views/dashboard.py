"""The overview screen: how you are doing, over a stretch of time you choose.

This screen used to show money in, money out and a category breakdown for
the entire ledger, all time. Those were technically numbers but not answers.
"You have spent $41,000 since 2024" is not something anybody can act on, and
the category bars repeated what the Spending screen already said, with no way
to change the period.

"How am I doing?" is always a comparative question, so everything here is a
period against the period before it. The picker chooses the period; every
figure carries which way it moved; and "What changed" names the handful of
categories that actually moved, because a number that has not changed is not
news and does not deserve the space.

Spending is drawn as proportional bars rather than a pie chart. A pie makes
adjacent slices almost impossible to compare, and comparing categories is
the entire job of the lower half of this screen.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...analysis import overview
from ...core.money import Money
from ..data import Ledger
from ..widgets import Card, StatCard, StatRow

CUSTOM = "Custom…"


def _clear(layout) -> None:
    """Remove and destroy everything in a layout, immediately.

    deleteLater() alone is not enough here: it defers destruction to the next
    event-loop turn, so the rebuilt rows are drawn on top of the old ones and
    the section renders as doubled, overlapping text. Reparenting to None
    detaches a widget there and then; nested layouts are cleared first, or
    their children are orphaned rather than removed.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())
            item.layout().deleteLater()


class _Bar(QFrame):
    """A single proportional bar. Width is set by the layout stretch factors."""

    def __init__(self, fraction: float, colour: str) -> None:
        super().__init__()
        self.setFixedHeight(8)
        self.setStyleSheet(f"background: {colour}; border-radius: 4px;")
        self.fraction = fraction


class DashboardView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        title = QLabel("Overview")
        title.setObjectName("Title")
        outer.addWidget(title)

        outer.addLayout(self._period_row())

        self.range_label = QLabel("")
        self.range_label.setObjectName("Subtitle")
        outer.addWidget(self.range_label)

        self.in_card = StatCard("Money in", "-")
        self.out_card = StatCard("Money out", "-")
        self.net_card = StatCard("Net", "-")
        self.burn_card = StatCard("Spent per day", "-")
        outer.addWidget(StatRow([self.in_card, self.out_card, self.net_card, self.burn_card]))

        # What changed: the point of the screen, so it sits above the detail.
        self.changes_card = Card()
        self.changes_layout = QVBoxLayout(self.changes_card)
        self.changes_layout.setContentsMargins(20, 16, 20, 16)
        self.changes_layout.setSpacing(6)
        outer.addWidget(self.changes_card)

        heading_row = QHBoxLayout()
        heading = QLabel("Spending by category")
        heading.setObjectName("SectionHeading")
        heading_row.addWidget(heading)
        heading_row.addStretch(1)

        # Only worth offering when guessing is on; otherwise it is a control
        # that does nothing, which is worse than no control.
        self.include_guesses = QCheckBox("Include guessed categories")
        self.include_guesses.setChecked(bool(ledger.setting("include_guesses_in_totals")))
        self.include_guesses.setCursor(Qt.CursorShape.PointingHandCursor)
        self.include_guesses.setToolTip(
            "Guessed categories are marked with ? in Transactions. Untick to see "
            "only what the rules matched; guessed rows fall back to Uncategorized "
            "rather than disappearing."
        )
        self.include_guesses.toggled.connect(self._toggle_guesses)
        heading_row.addWidget(self.include_guesses)
        outer.addLayout(heading_row)

        self.categories_card = Card()
        self.categories_layout = QGridLayout(self.categories_card)
        self.categories_layout.setContentsMargins(20, 16, 20, 16)
        self.categories_layout.setHorizontalSpacing(14)
        self.categories_layout.setVerticalSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.categories_card)
        outer.addWidget(scroll, stretch=1)

        self.refresh()

    # -- the period picker -----------------------------------------------

    def _period_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.preset = QComboBox()
        self.preset.addItems([*overview.PRESETS, CUSTOM])
        saved = str(self.ledger.setting("overview_period") or overview.DEFAULT_PRESET)
        if saved not in [*overview.PRESETS, CUSTOM]:
            saved = overview.DEFAULT_PRESET
        self.preset.setCurrentText(saved)
        self.preset.currentTextChanged.connect(self._preset_changed)
        row.addWidget(self.preset)

        self.from_date = QDateEdit()
        self.to_date = QDateEdit()
        for field in (self.from_date, self.to_date):
            field.setCalendarPopup(True)
            field.setDisplayFormat("yyyy-MM-dd")
            field.dateChanged.connect(lambda _: self._dates_changed())

        first, last = self._saved_custom()
        self.from_date.setDate(QDate(first))
        self.to_date.setDate(QDate(last))

        self.from_label = QLabel("from")
        self.to_label = QLabel("to")
        for widget in (self.from_label, self.from_date, self.to_label, self.to_date):
            widget.setObjectName("Muted" if isinstance(widget, QLabel) else "")
            row.addWidget(widget)

        row.addStretch(1)
        self._show_custom(saved == CUSTOM)
        return row

    def _saved_custom(self) -> tuple[date, date]:
        stored = self.ledger.setting("overview_custom_range") or []
        today = date.today()
        try:
            return date.fromisoformat(stored[0]), date.fromisoformat(stored[1])
        except (IndexError, TypeError, ValueError):
            return date(today.year, today.month, 1), today

    def _show_custom(self, shown: bool) -> None:
        for widget in (self.from_label, self.from_date, self.to_label, self.to_date):
            widget.setVisible(shown)

    def _preset_changed(self, name: str) -> None:
        self.ledger.save_setting("overview_period", name)
        self._show_custom(name == CUSTOM)
        self.refresh()

    def _dates_changed(self) -> None:
        if self.preset.currentText() != CUSTOM:
            return
        self.ledger.save_setting(
            "overview_custom_range",
            [
                self.from_date.date().toPython().isoformat(),
                self.to_date.date().toPython().isoformat(),
            ],
        )
        self.refresh()

    def _period(self) -> tuple[overview.Period, overview.Period | None]:
        name = self.preset.currentText()
        today = date.today()
        if name == CUSTOM:
            first = self.from_date.date().toPython()
            last = self.to_date.date().toPython()
            # A range typed backwards is a slip, not a request for nothing.
            if first > last:
                first, last = last, first
            current = overview.Period(first, last)
            return current, current.before()
        dates = [t.date for t in self.ledger.transactions]
        return overview.preset(name, today, min(dates) if dates else today)

    # -- rendering --------------------------------------------------------

    def _toggle_guesses(self, included: bool) -> None:
        self.ledger.save_setting("include_guesses_in_totals", included)
        self.refresh()

    def refresh(self) -> None:
        transactions = self.ledger.transactions
        if not transactions:
            self.range_label.setText("No transactions imported yet.")
            return

        show_guesses = bool(self.ledger.setting("include_guesses_in_totals"))
        # Hidden rather than disabled when guessing is off: the question it
        # answers does not exist then.
        self.include_guesses.setVisible(bool(self.ledger.setting("auto_categorize")))
        self.include_guesses.blockSignals(True)
        self.include_guesses.setChecked(show_guesses)
        self.include_guesses.blockSignals(False)

        period, previous = self._period()
        categories = {
            t.id: self.ledger.category_of(t, include_guessed=show_guesses) for t in transactions
        }
        summary = overview.summarise(transactions, categories, period, previous)

        note = f"{period.describe()}  ·  {summary.count:,} transactions"
        if previous is not None:
            note += f"  ·  compared with {previous.describe()}"
        self.range_label.setText(note)

        self._render_stats(summary)
        self._render_changes(summary)
        self._render_categories(summary)

    def _render_stats(self, summary: overview.Summary) -> None:
        self.in_card.set_value(summary.earned.format())
        self.out_card.set_value(abs(summary.spent).format())
        self.net_card.set_value(summary.net.format())
        self.burn_card.set_value(summary.daily_burn.format())

        if summary.previous is None or summary.count == 0:
            # With nothing in the period there is nothing to compare. The
            # arithmetic would still produce numbers -- "net up 100%", "spending
            # down 100%" -- and every one of them would read as good news about
            # a month that has simply not happened yet.
            for card in (self.in_card, self.out_card, self.net_card, self.burn_card):
                card.set_comparison("")
            return

        # More money in is good; more money out is not. The same arrow means
        # opposite things on the two cards, so they get opposite colours.
        self.in_card.set_comparison(*_delta(summary.earned, summary.previous_earned, better="up"))
        self.out_card.set_comparison(
            *_delta(
                abs(summary.spent),
                abs(summary.previous_spent) if summary.previous_spent is not None else None,
                better="down",
            )
        )
        self.net_card.set_comparison(*_delta(summary.net, summary.previous_net, better="up"))
        previous_burn = (
            Money(abs(summary.previous_spent.minor) // max(summary.previous.days, 1))
            if summary.previous_spent is not None
            else None
        )
        self.burn_card.set_comparison(*_delta(summary.daily_burn, previous_burn, better="down"))

    def _render_changes(self, summary: overview.Summary) -> None:
        _clear(self.changes_layout)

        heading = QLabel("What changed")
        heading.setObjectName("SectionHeading")
        self.changes_layout.addWidget(heading)

        if summary.previous is None:
            note = QLabel("Pick a period other than All time to see what moved.")
            note.setObjectName("Muted")
            self.changes_layout.addWidget(note)
            return
        if summary.count == 0:
            # Every category would otherwise be reported as having "stopped",
            # which is a strong claim to make about a month that began today.
            note = QLabel(
                "Nothing recorded in this period yet — so there is nothing to "
                "compare. Refresh from your banks, or pick a longer period."
            )
            note.setObjectName("Muted")
            note.setWordWrap(True)
            self.changes_layout.addWidget(note)
            return
        if not summary.movements:
            note = QLabel("Nothing moved much. Spending looks like last time.")
            note.setObjectName("Muted")
            self.changes_layout.addWidget(note)
            return

        for move in summary.movements:
            row = QHBoxLayout()
            name = QLabel(move.category)
            row.addWidget(name)
            row.addStretch(1)

            if move.is_new:
                text = f"new — {abs(move.now).format()}"
            elif move.is_gone:
                text = f"stopped — was {abs(move.before).format()}"
            else:
                arrow = "↑" if move.rose else "↓"
                percent = f" ({move.percent:+.0f}%)" if move.percent is not None else ""
                text = f"{arrow} {abs(move.change).format()}{percent}"

            value = QLabel(text)
            value.setObjectName("Danger" if move.rose else "Accent")
            row.addWidget(value)

            was = QLabel(f"{abs(move.before).format()} → {abs(move.now).format()}")
            was.setObjectName("Muted")
            row.addWidget(was)
            self.changes_layout.addLayout(row)

    def _render_categories(self, summary: overview.Summary) -> None:
        _clear(self.categories_layout)
        rows = summary.categories
        if not rows:
            empty = QLabel("Nothing spent in this period.")
            empty.setObjectName("Muted")
            self.categories_layout.addWidget(empty, 0, 0)
            return
        largest = abs(rows[0][1].minor) or 1

        # One accent hue, stepped in lightness. Distinct colours per category
        # would imply the categories are unrelated; they are all spending.
        for index, (name, amount, count) in enumerate(rows):
            fraction = abs(amount.minor) / largest
            shade = 45 + int(25 * (1 - fraction))

            label = QLabel(name)
            value = QLabel(amount.format())
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            note = QLabel(f"{count} txns")
            note.setObjectName("Muted")

            track = QWidget()
            track_layout = QVBoxLayout(track)
            track_layout.setContentsMargins(0, 0, 0, 0)
            bar = _Bar(fraction, f"hsl(145, 55%, {shade}%)")
            bar.setMinimumWidth(max(6, int(240 * fraction)))
            bar.setMaximumWidth(max(6, int(240 * fraction)))
            track_layout.addWidget(bar)

            self.categories_layout.addWidget(label, index, 0)
            self.categories_layout.addWidget(track, index, 1)
            self.categories_layout.addWidget(value, index, 2)
            self.categories_layout.addWidget(note, index, 3)

        self.categories_layout.setColumnStretch(0, 2)
        self.categories_layout.setColumnStretch(1, 3)


def _delta(now: Money, before: Money | None, *, better: str) -> tuple[str, str]:
    """A one-line comparison, and the object name it should be styled with.

    `better` says which direction is the good one, because more money in and
    more money out are both "up" and mean opposite things.
    """
    if before is None:
        return "", "Muted"
    if before.minor == 0:
        # No base to compare against. A percentage would be an infinity.
        return ("nothing last time" if now.minor else "same as last time"), "Muted"

    change = now.minor - before.minor
    if change == 0:
        return "same as last time", "Muted"

    percent = change / abs(before.minor) * 100
    arrow = "↑" if change > 0 else "↓"
    improved = (change > 0) if better == "up" else (change < 0)
    amount = Money(abs(change), now.currency)
    return (
        f"{arrow} {amount.format()} ({percent:+.0f}%) on last time",
        "Accent" if improved else "Danger",
    )
