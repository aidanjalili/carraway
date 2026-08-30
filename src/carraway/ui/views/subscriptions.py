"""The subscriptions screen — the reason this project exists.

Everything else in a money app has an open source equivalent already. This
screen does not: a plain list of what recurs, what it costs a year, when the
next charge lands, and which ones look like they quietly stopped.

The headline number is the annual figure rather than the monthly one, because
$9.99 a month reads as nothing and $119.88 a year reads as a decision.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.models import RecurringSeries
from ...core.money import Money, total
from ..data import Ledger
from ..widgets import (
    FilterStrip,
    SortableItem,
    StatCard,
    StatRow,
    enable_row_hover,
)
from . import add_subscription, edit_series
from .classify_dialog import ClassifyDialog

_HEADERS = [
    "Merchant",
    "Kind",
    "Cadence",
    "Amount",
    "Per year",
    "Next charge",
    "Seen",
    "Confidence",
]

# Sort order for the Kind column: what you can cancel first, what you have
# not yet decided about last, since that is the row needing an action.
# Ordered by period length, so sorting runs weekly, biweekly, monthly,
# quarterly, yearly rather than alphabetically — "biweekly, monthly, quarterly,
# weekly, yearly" is alphabetical and useless.
_CADENCE_ORDER = {
    "weekly": 0,
    "biweekly": 1,
    "monthly": 2,
    "quarterly": 3,
    "yearly": 4,
}

_KIND_ORDER = {
    "subscription": 0,
    "bill": 1,
    "income": 2,
    "habit": 3,
    "cancelled": 4,
    "unknown": 5,
}


def _cadence_label(series: RecurringSeries) -> str:
    # A biweekly charge is 26 payments a year, not 24. People consistently
    # underestimate these, so the count is spelled out rather than implied.
    per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
    count = per_year.get(series.cadence, 0)
    suffix = " ~" if series.amount_varies else ""
    return f"{series.cadence} ({count}x){suffix}" if count else series.cadence


class SubscriptionsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("Subscriptions")
        title.setObjectName("Title")
        subtitle = QLabel("Everything that charges you on a schedule.")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.count_card = StatCard("Subscriptions", "0")
        self.monthly_card = StatCard("Per month", "-")
        self.yearly_card = StatCard("Per year", "-", tone="Accent")
        self.stale_card = StatCard("Unclassified", "0")
        layout.addWidget(
            StatRow([self.count_card, self.monthly_card, self.yearly_card, self.stale_card])
        )

        # A price rise is the single most actionable thing this app can tell
        # someone, so it gets its own line above the table rather than being a
        # column they have to notice.
        self.price_notice = QLabel("")
        self.price_notice.setObjectName("Danger")
        self.price_notice.setWordWrap(True)
        self.price_notice.setVisible(False)
        layout.addWidget(self.price_notice)

        # Bills, subscriptions and stopped things answer different questions,
        # so they get their own tabs rather than one list the user must scan.
        tab_row = QHBoxLayout()
        self.tabs = FilterStrip()
        for label in (
            "Subscriptions",
            "Bills",
            "Income",
            "Habits",
            "Cancelled",
            "Stopped",
            "All",
            "Hidden",
        ):
            self.tabs.addTab(label)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        tab_row.addWidget(self.tabs)
        tab_row.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search merchant, kind or cadence…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(lambda _: self._apply_search())
        tab_row.addWidget(self.search)

        add = QPushButton("Track one manually…")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_manual)
        tab_row.addWidget(add)
        layout.addLayout(tab_row)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignLeft if column < 3 else Qt.AlignmentFlag.AlignRight
            ) | Qt.AlignmentFlag.AlignVCenter
            self.table.horizontalHeaderItem(column).setTextAlignment(align)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # Mouse tracking so the row under the cursor repaints without a
        # click; without it Qt only updates on press.
        # Row-wide hover; Qt's stylesheet :hover only covers one cell.
        self._hover = enable_row_hover(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.filter_note = QLabel("")
        self.filter_note.setObjectName("Muted")
        self.filter_note.setVisible(False)
        layout.addWidget(self.filter_note)

        self.search_note = QLabel("")
        self.search_note.setObjectName("Muted")
        self.search_note.setVisible(False)
        layout.addWidget(self.search_note)

        self.totals = QLabel("")
        self.totals.setObjectName("SectionHeading")
        layout.addWidget(self.totals)

        self.footnote = QLabel("")
        self.footnote.setObjectName("Muted")
        self.footnote.setWordWrap(True)
        layout.addWidget(self.footnote)

        self.review_button = QPushButton("Classify the unclassified…")
        self.review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.review_button.clicked.connect(self._review_unclassified)
        layout.addWidget(self.review_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _: self._edit_selected())

        self.refresh()

    def _apply_search(self) -> None:
        """Hide rows the search text does not appear in.

        Rows are hidden rather than the table rebuilt, so sorting survives and
        the running totals keep describing the tab rather than the search.
        Matched across merchant, kind and cadence at once, since typing
        "yearly" or "bill" is as natural as typing a merchant name.
        """
        needle = self.search.text().strip().lower()
        hidden = 0
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            cells = (self.table.item(row, column) for column in (0, 1, 2))
            keep = any(cell and needle in cell.text().lower() for cell in cells)
            self.table.setRowHidden(row, not keep)
            hidden += not keep

        if not needle or not hidden:
            self.search_note.setVisible(False)
            return
        shown = self.table.rowCount() - hidden
        self.search_note.setText(
            f"{shown} of {self.table.rowCount()} match \u201c{self.search.text().strip()}\u201d"
            "  ·  the totals above still describe the whole tab"
        )
        self.search_note.setVisible(True)

    def _visible(self) -> list:
        """The series belonging to the selected tab."""
        chosen = self.tabs.tabText(self.tabs.currentIndex())
        stale = {id(s) for s in self.ledger.stale_series}
        everything = self.ledger.series
        if chosen == "Hidden":
            # The one tab that shows dismissed series, so a mistake is
            # reversible rather than a one-way door.
            return self.ledger.dismissed
        if chosen == "All":
            return everything
        if chosen == "Stopped":
            # Anything whose expected charge never arrived, whatever its kind:
            # "did this quietly stop?" is its own question.
            return [s for s in everything if id(s) in stale]

        wanted = {
            "Subscriptions": "subscription",
            "Bills": "bill",
            "Income": "income",
            "Habits": "habit",
            "Cancelled": "cancelled",
        }[chosen]
        return [s for s in everything if self.ledger.kind_of(s) == wanted and id(s) not in stale]

    def refresh(self) -> None:
        series = self._visible()
        active = self.ledger.active_series
        stale = self.ledger.stale_series
        today = date.today()

        # The headline is what the user could actually cancel. Rent and
        # utilities recur just as reliably and belong in a different column of
        # someone's thinking, so they are counted separately.
        # Only what is both cancellable and still charging: a subscription the
        # user has already cancelled is not money they are paying.
        cancellable = [s for s in active if self.ledger.kind_of(s) == "subscription"]
        unknown = [s for s in self.ledger.series if self.ledger.kind_of(s) == "unknown"]

        # Headline totals use current prices too, or a subscription that went
        # up in March would still be counted at its old rate.
        yearly = total([self.ledger.current_annual(s) for s in cancellable])
        self.count_card.set_value(str(len(cancellable)))
        self.monthly_card.set_value(Money(round(yearly.minor / 12), yearly.currency).format())
        self.yearly_card.set_value(yearly.format())
        self.stale_card.set_value(str(len(unknown)))

        # Sorting has to be off while filling, or rows reorder underneath the
        # loop and land in the wrong places.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(series))
        # Row order changes when the table sorts, so the series a row shows
        # is carried on the row itself rather than inferred from its index.
        self._rows: dict[int, object] = {}
        stale_ids = {id(s) for s in stale}

        for row, item in enumerate(series):
            gone = id(item) in stale_ids
            name = item.merchant + ("  (stopped?)" if gone else "")
            kind = self.ledger.kind_of(item)
            change = self.ledger.price_change_for(item)
            if change is not None:
                name += "  ↑" if change.direction == "increase" else "  ↓"
            if self.ledger.is_edited(item):
                name += "  ✎"
            # Show what the series costs now rather than its historical median,
            # which is stale the moment a price changes.
            amount = self.ledger.current_amount(item)
            annual = self.ledger.current_annual(item)
            cells = [
                SortableItem(name, item.merchant.lower()),
                # Sorted by kind, then by cost within a kind, so the
                # default view reads as grouped sections rather than one
                # list with a $59k payroll sitting on top of Netflix.
                SortableItem(kind, (_KIND_ORDER.get(kind, 9), -abs(item.annualised.minor))),
                # Sorted by how often it bills, then by cost within a
                # cadence. Sorting this column by annual cost — which it did —
                # makes clicking the Cadence header do nothing visible.
                SortableItem(
                    _cadence_label(item),
                    (_CADENCE_ORDER.get(item.cadence, 99), -abs(annual.minor)),
                ),
                SortableItem(abs(amount).format(), abs(amount.minor)),
                SortableItem(annual.format(), annual.minor),
                SortableItem(
                    item.next_expected.isoformat() if item.next_expected else "-",
                    item.next_expected.toordinal() if item.next_expected else 0,
                ),
                SortableItem(str(item.occurrences), item.occurrences),
                SortableItem(f"{item.confidence:.0%}", item.confidence),
            ]
            self._rows[id(item)] = item
            cells[0].setData(Qt.ItemDataRole.UserRole, id(item))
            for column, cell in enumerate(cells):
                if column >= 3:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if gone:
                    cell.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(row, column, cell)

        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.AscendingOrder)
        self._apply_search()

        # A running total for whatever is on screen, which is the number
        # someone actually came to this tab for.
        shown_year = total([self.ledger.current_annual(s) for s in series])
        shown_month = Money(round(shown_year.minor / 12), shown_year.currency)
        label = self.tabs.tabText(self.tabs.currentIndex())
        self.totals.setText(
            f"{label}: {len(series)} · {shown_month.format()} per month · "
            f"{shown_year.format()} per year"
        )

        varies = sum(1 for s in series if s.amount_varies)
        notes = [f"{len(series)} series detected as of {today.isoformat()}"]
        if unknown:
            notes.append(f"{len(unknown)} unclassified — run 'carraway review'")
        if varies:
            notes.append(f"~ marks {varies} whose amount changes between charges")
        if stale:
            notes.append(f"{len(stale)} greyed out — expected charge never arrived")
        rises = [c for c in self.ledger.price_changes if c.direction == "increase"]
        if rises:
            biggest = max(rises, key=lambda c: abs(c.annual_impact.minor))
            extra = total([abs(c.annual_impact) for c in rises])
            lead = (
                f"{biggest.merchant} went from {abs(biggest.old_amount).format()} to "
                f"{abs(biggest.new_amount).format()} on {biggest.changed_on}"
            )
            more = f" — and {len(rises) - 1} other rose too" if len(rises) > 1 else ""
            self.price_notice.setText(
                f"↑ {lead}{more}. Price rises are costing you {extra.format()}/year more."
            )
            self.price_notice.setVisible(True)
        else:
            self.price_notice.setVisible(False)

        self.footnote.setText("   ·   ".join(notes))
        pending = len(unknown)
        self.review_button.setText(
            f"Classify {pending} unclassified…" if pending else "Reclassify a merchant…"
        )
        self.review_button.setEnabled(bool(series))

    # -- classification --------------------------------------------------

    def _series_at(self, row: int):
        """The series a table row is showing, or None."""
        item = self.table.item(row, 0)
        if item is None:
            return None
        return self._rows.get(item.data(Qt.ItemDataRole.UserRole))

    def _context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        series = self._series_at(row)
        if series is None:
            return
        menu = QMenu(self)
        classify = QAction(f"What is {series.merchant}?", self)
        classify.triggered.connect(lambda: self._classify(series))
        menu.addAction(classify)

        edit = QAction(f"Edit {series.merchant}…", self)
        edit.triggered.connect(lambda: self._edit(series))
        menu.addAction(edit)

        menu.addSeparator()
        dismiss = QAction("Not recurring — hide this", self)
        dismiss.setToolTip(
            "For when detection was wrong. It stays hidden through future "
            "imports, and can be restored from the Hidden tab."
        )
        dismiss.triggered.connect(lambda: self._dismiss(series))
        menu.addAction(dismiss)

        if self.ledger.is_manual(series):
            # Only a tracked entry can be removed. A detected one is a fact
            # about the ledger, and deleting it would just mean re-detecting it.
            menu.addSeparator()
            stop = QAction(f"Stop tracking {series.merchant}", self)
            stop.setToolTip(
                "Keeps it on record as something you used to pay for, and stops "
                "counting it towards your totals."
            )
            stop.triggered.connect(lambda: self._remove_manual(series))
            menu.addAction(stop)

            delete = QAction(f"Delete {series.merchant} entirely…", self)
            delete.setToolTip("For one added by mistake. This cannot be undone.")
            delete.triggered.connect(lambda: self._delete_manual(series))
            menu.addAction(delete)

        menu.exec(self.table.viewport().mapToGlobal(position))

    def _add_manual(self) -> None:
        """Record a subscription no detector can see."""
        values = add_subscription.prompt(self)
        if values is None:
            return
        self.ledger.add_manual(values)
        self.refresh()

    def _edit(self, series) -> None:
        corrections = edit_series.prompt(series, self.ledger.is_edited(series), self)
        if corrections is None:
            return
        if corrections == {}:
            self.ledger.reset_series(series)
        else:
            self.ledger.edit_series(series, **corrections)
        self.refresh()

    def _dismiss(self, series) -> None:
        self.ledger.dismiss(series)
        self.refresh()

    def _restore(self, series) -> None:
        self.ledger.restore(series)
        self.ledger.load()
        self.refresh()

    def _delete_manual(self, series) -> None:
        """Remove a tracked entry for good, after asking.

        Distinct from stopping tracking: that keeps a real subscription on
        record, whereas this is for an entry that should never have existed.
        Irreversible, so it asks.
        """
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self,
            "Delete this entry?",
            f"Delete “{series.merchant}” entirely?\n\n"
            "This is for an entry added by mistake and cannot be undone. To "
            "keep it on record as something you used to pay for, choose "
            "“Stop tracking” instead.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes and self.ledger.delete_manual(series):
            self.refresh()

    def _remove_manual(self, series) -> None:
        if self.ledger.remove_manual(series):
            self.refresh()

    def _edit_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        series = self._series_at(rows[0].row())
        if series is not None:
            self._edit(series)

    def _classify_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        series = self._series_at(rows[0].row())
        if series is not None:
            self._classify(series)

    def _classify(self, series) -> bool:
        """Ask about one merchant. Returns False if the user cancelled."""
        dialog = ClassifyDialog(series, self.ledger.kind_of(series), self)
        if dialog.exec() != ClassifyDialog.DialogCode.Accepted:
            return False
        self.ledger.set_kind(series, dialog.chosen)
        self.refresh()
        return True

    def _review_unclassified(self) -> None:
        """Walk the unclassified merchants, most expensive first.

        Ordered by annual cost so the first question asked is always the one
        worth the most, and stops as soon as the user cancels rather than
        marching them through a queue they wanted out of.
        """
        pending = [s for s in self.ledger.series if self.ledger.kind_of(s) == "unknown"]
        pending.sort(key=lambda s: -abs(s.annualised.minor))
        if not pending:
            # Nothing outstanding, so offer to revisit the selected row instead.
            self._classify_selected()
            return
        for series in pending:
            if not self._classify(series):
                break
