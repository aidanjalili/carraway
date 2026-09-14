"""Where the money went, at whatever zoom you want.

One period at a time, steppable, with the same numbers shown as a pie, as bars
or as a table. The chart type is a genuine preference rather than a gimmick: a
pie reads shares, bars compare sizes accurately, and a table is the only one
you can copy a figure out of.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...analysis import spending
from ...core.money import Money
from ..data import Ledger
from ..widgets import (
    Card,
    SortableItem,
    StatCard,
    StatRow,
    enable_row_hover,
    export_button,
    mark_favourite,
    plain_category,
    resizable_columns,
)
from .charts import BarChart, PieChart, Slice, TrendChart

_HEADERS = ["Category", "Spent", "Share", "Transactions"]


class SpendingView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self.buckets: list[spending.Bucket] = []
        self.index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Spending")
        title.setObjectName("Title")
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.granularity = QComboBox()
        self.granularity.addItems(["monthly", "weekly", "daily", "yearly"])
        self.granularity.currentTextChanged.connect(self._reload)
        controls.addWidget(QLabel("Show"))
        controls.addWidget(self.granularity)

        self.previous = QPushButton("‹")
        self.previous.setFixedWidth(34)
        self.previous.clicked.connect(lambda: self._step(-1))
        self.period_label = QLabel("")
        self.period_label.setObjectName("SectionHeading")
        self.period_label.setMinimumWidth(150)
        self.period_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next = QPushButton("›")
        self.next.setFixedWidth(34)
        self.next.clicked.connect(lambda: self._step(1))
        controls.addSpacing(12)
        controls.addWidget(self.previous)
        controls.addWidget(self.period_label)
        controls.addWidget(self.next)
        controls.addStretch(1)

        # Chart type as exclusive buttons rather than a dropdown: there are
        # three, and switching between them is the whole point of the screen.
        self.chart_buttons = QButtonGroup(self)
        self.chart_buttons.setExclusive(True)
        for position, name in enumerate(("Pie", "Bars", "Table", "Trend")):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(position == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.chart_buttons.addButton(button, position)
            controls.addWidget(button)
        self.chart_buttons.idClicked.connect(self._show_chart)
        layout.addLayout(controls)

        # The same question the Overview asks, on the screen where the
        # breakdown is actually studied. One setting behind both, so answering
        # it in either place answers it everywhere.
        guess_row = QHBoxLayout()
        guess_row.addStretch(1)
        self.include_guesses = QCheckBox("Include guessed categories")
        self.include_guesses.setChecked(bool(ledger.setting("include_guesses_in_totals")))
        self.include_guesses.setCursor(Qt.CursorShape.PointingHandCursor)
        self.include_guesses.setToolTip(
            "Guessed categories are marked with ? in Transactions. Untick to see "
            "only what the rules matched; guessed rows fall back to Uncategorized "
            "rather than disappearing."
        )
        self.include_guesses.toggled.connect(self._toggle_guesses)
        guess_row.addWidget(self.include_guesses)

        # The categories someone starred are the ones they are watching, and
        # the question this answers is "how am I doing on the things I care
        # about" -- which a pie with Rent/Mortgage and Uncategorized taking
        # half of it does not.
        self.favourites_only = QCheckBox("Favourites only")
        self.favourites_only.setChecked(bool(ledger.setting("spending_favourites_only")))
        self.favourites_only.setCursor(Qt.CursorShape.PointingHandCursor)
        self.favourites_only.toggled.connect(self._toggle_favourites)
        guess_row.addWidget(self.favourites_only)
        layout.addLayout(guess_row)

        self.total_card = StatCard("Spent this period", "-")
        self.average_card = StatCard("Average per period", "-")
        self.biggest_card = StatCard("Biggest category", "-")
        self.count_card = StatCard("Transactions", "-")
        layout.addWidget(
            StatRow([self.total_card, self.average_card, self.biggest_card, self.count_card])
        )

        board = Card()
        board_layout = QVBoxLayout(board)
        board_layout.setContentsMargins(12, 12, 12, 12)

        self.pie = PieChart()
        self.bars = BarChart()
        self.trend = TrendChart()
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # Mouse tracking so the row under the cursor repaints without a
        # click; without it Qt only updates on press.
        # Row-wide hover; Qt's stylesheet :hover only covers one cell.
        self._hover = enable_row_hover(self.table)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        resizable_columns(self.table, ledger, "spending", stretch=0)

        # Goes in the controls row at the top, but is built here because it
        # needs the table and the table is made last. It stays put under Pie
        # and Bars as well: those draw the same rows this table holds, so the
        # figures behind whichever chart is up are the ones that come out.
        controls.addWidget(export_button(self.table, self, "spending"))

        self.stack = QStackedWidget()
        for widget in (self.pie, self.bars, self.table, self.trend):
            self.stack.addWidget(widget)
        board_layout.addWidget(self.stack)
        layout.addWidget(board, stretch=1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self._reload()

    # -- data ------------------------------------------------------------

    def _toggle_favourites(self, only: bool) -> None:
        self.ledger.save_setting("spending_favourites_only", only)
        self._draw()

    def _scope(self) -> set[str] | None:
        """The categories this screen is limited to, or None for all of them.

        None rather than an empty set when there are no favourites, so the
        filter can never silently empty the screen: a box that is ticked but
        has nothing to keep reads as "you spent nothing", which is a lie.
        """
        favourites = self.ledger.favourite_categories
        if not favourites or not self.favourites_only.isChecked():
            return None
        return favourites

    def _scoped(self, bucket):
        """(by_category, total) for one period, with the filter applied."""
        scope = self._scope()
        if scope is None:
            return dict(bucket.by_category), bucket.total
        kept = {k: v for k, v in bucket.by_category.items() if k in scope}
        return kept, Money(sum(v.minor for v in kept.values()), bucket.total.currency)

    def _toggle_guesses(self, included: bool) -> None:
        self.ledger.save_setting("include_guesses_in_totals", included)
        self._reload()

    def _reload(self) -> None:
        show_guesses = bool(self.ledger.setting("include_guesses_in_totals"))
        # Hidden when guessing is off: a control that cannot change anything is
        # worse than no control.
        self.include_guesses.setVisible(bool(self.ledger.setting("auto_categorize")))
        self.include_guesses.blockSignals(True)
        self.include_guesses.setChecked(show_guesses)
        self.include_guesses.blockSignals(False)

        period = self.granularity.currentText()
        self.buckets = self.ledger.spending_buckets(period, include_guessed=show_guesses)
        # Land on the most recent period: that is what someone opening this
        # screen wants, not the oldest month in their history.
        self.index = len(self.buckets) - 1 if self.buckets else 0
        self._draw()

    def _step(self, direction: int) -> None:
        if not self.buckets:
            return
        self.index = max(0, min(len(self.buckets) - 1, self.index + direction))
        self._draw()

    def _show_chart(self, position: int) -> None:
        self.stack.setCurrentIndex(position)
        self._draw()

    def refresh(self) -> None:
        """Recount from the ledger, staying on the period being looked at.

        This is what the window calls after the ledger changes -- a sync, a
        category set by hand, a rename, a collection from the phone -- and it
        used to redraw the periods counted when the screen was built. Filing
        a $900 charge under a different category left this screen showing it
        where it had been until the app was restarted.
        """
        showing = self.buckets[self.index].start if self.buckets else None
        show_guesses = bool(self.ledger.setting("include_guesses_in_totals"))
        self.include_guesses.setVisible(bool(self.ledger.setting("auto_categorize")))
        self.buckets = self.ledger.spending_buckets(
            self.granularity.currentText(), include_guessed=show_guesses
        )
        starts = [bucket.start for bucket in self.buckets]
        self.index = (
            starts.index(showing)
            if showing in starts
            else (len(self.buckets) - 1 if self.buckets else 0)
        )
        self._draw()

    def _draw(self) -> None:
        favourites = self.ledger.favourite_categories
        # Disabled, not hidden, when nothing is starred: the control is worth
        # discovering, and the tooltip says where the stars come from.
        self.favourites_only.setEnabled(bool(favourites))
        self.favourites_only.setToolTip(
            "Show only the categories you starred."
            if favourites
            else "Star categories in Settings -> Categories to use this."
        )

        if not self.buckets:
            self.period_label.setText("no data")
            self.footnote.setText("Import or sync some transactions to see this.")
            return

        scope = self._scope()
        bucket = self.buckets[self.index]
        by_category, total = self._scoped(bucket)
        rows = sorted(by_category.items(), key=lambda kv: -kv[1].minor)
        slices = [
            Slice(
                # Starred ones marked while everything is showing. With the
                # filter on every slice is a favourite, and a star on all of
                # them says nothing.
                label=name if scope is not None else mark_favourite(name, favourites),
                amount=amount,
                fraction=(amount.minor / total.minor) if total.minor else 0.0,
            )
            for name, amount in rows
        ]

        self.pie.set_slices(slices)
        self.bars.set_slices(slices)
        # The trend chart ignores the selected period and shows every one, so
        # it answers "is this month unusual?" rather than "what was in it?".
        self.trend.set_slices(
            [
                Slice(label=b.label, amount=self._scoped(b)[1], fraction=0.0)
                for b in self.buckets[-60:]
            ]
        )
        self._fill_table(slices, total)

        self.period_label.setText(bucket.label)
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index < len(self.buckets) - 1)

        self.total_card.set_value(total.format())
        spent = [t for t in (self._scoped(b)[1] for b in self.buckets) if t.minor]
        average = (
            Money(round(sum(b.minor for b in spent) / len(spent)), total.currency)
            if spent
            else Money.zero()
        )
        self.average_card.set_value(average.format())
        self.biggest_card.set_value(rows[0][0] if rows else "-")
        self.count_card.set_value(str(self._transaction_count(bucket, scope)))

        change = ""
        if self.index > 0:
            previous = self.buckets[self.index - 1]
            before = self._scoped(previous)[1]
            delta = total.minor - before.minor
            if before.minor:
                pct = 100 * delta / before.minor
                direction = "more" if delta > 0 else "less"
                change = (
                    f"{Money(abs(delta), total.currency).format()} {direction} "
                    f"than {previous.label} ({pct:+.0f}%)"
                )
        parts = [f"{len(self.buckets)} periods on record"]
        if scope is not None:
            # Scoped figures with nothing to anchor them read as the whole
            # month. Saying what share of it they are keeps the filter honest.
            parts.insert(
                0,
                f"your {len(scope)} starred categories: {total.format()} "
                f"of {bucket.total.format()} spent",
            )
        if change:
            parts.append(change)
        self.footnote.setText("   ·   ".join(parts))

    def _transaction_count(self, bucket, scope: set[str] | None = None) -> int:
        include = bool(self.ledger.setting("include_guesses_in_totals"))
        return sum(
            1
            for t in self.ledger.transactions
            if bucket.start <= t.date < bucket.end
            and t.is_outflow
            and not t.is_transfer
            and (scope is None or self.ledger.category_of(t, include_guessed=include) in scope)
        )

    def _fill_table(self, slices: list[Slice], total: Money) -> None:
        counts: dict[str, int] = {}
        bucket = self.buckets[self.index]
        for transaction in self.ledger.transactions:
            if not (bucket.start <= transaction.date < bucket.end):
                continue
            if not transaction.is_outflow or transaction.is_transfer:
                continue
            name = self.ledger.category_of(
                transaction,
                include_guessed=bool(self.ledger.setting("include_guesses_in_totals")),
            )
            counts[name] = counts.get(name, 0) + 1

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(slices))
        for row, item in enumerate(slices):
            name = plain_category(item.label)
            cells = [
                SortableItem(item.label, name.lower()),
                SortableItem(item.amount.format(), item.amount.minor),
                SortableItem(f"{item.fraction:.1%}", item.fraction),
                SortableItem(str(counts.get(name, 0)), counts.get(name, 0)),
            ]
            for column, cell in enumerate(cells):
                if column:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, cell)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)
