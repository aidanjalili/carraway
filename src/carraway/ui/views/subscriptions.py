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
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
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
from .. import theme
from ..data import Ledger
from ..widgets import (
    FilterStrip,
    PanelSplitter,
    SortableItem,
    StatCard,
    StatRow,
    enable_row_hover,
    export_button,
    refresh_everything,
    resizable_columns,
)
from . import add_subscription, billing_date, edit_series, paid_with
from .classify_dialog import ClassifyDialog

_HEADERS = [
    "Merchant",
    "Kind",
    "Cadence",
    "Paid with",
    "Amount",
    "Per year",
    "Next charge",
    "Seen",
    "Confidence",
]

# Everything from here rightwards is a number and is right-aligned; everything
# before it is text and is left-aligned. Named rather than written as a bare
# index so inserting a column means changing one number, not hunting for the
# comparisons that assumed the old layout.
_FIRST_NUMERIC_COLUMN = _HEADERS.index("Amount")

# The columns the search box looks in. Merchant, kind, cadence and the account
# it bills to: typing "wells fargo" to see everything on one card is as natural
# as typing a merchant name.
_SEARCHABLE_COLUMNS = (0, 1, 2, 3)

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


_PRICE_HEADERS = ["Merchant", "Was", "Now", "Changed", "Cadence", "Per year"]


class SubscriptionsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("Recurring")
        title.setObjectName("Title")
        subtitle = QLabel("Everything that charges you — or pays you — on a schedule.")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # "Recurring", not "Subscriptions": this screen has held bills, income
        # and habits for a while, and the card counts whatever the chips are
        # filtered to -- so under the Bills chip it was captioning a count of
        # bills as subscriptions.
        self.count_card = StatCard("Recurring", "0")
        self.monthly_card = StatCard("Per month", "-")
        self.yearly_card = StatCard("Per year", "-", tone="Accent")
        self.stale_card = StatCard("Unclassified", "0")
        layout.addWidget(
            StatRow([self.count_card, self.monthly_card, self.yearly_card, self.stale_card])
        )

        # A price rise is the most actionable thing this app can tell someone,
        # so the headline stays in the open. What used to sit here was that
        # headline and nothing else -- one merchant named, the rest summed
        # into "and N others rose too", with no way to find out which. The
        # history is now behind the same line, one click down, so the screen
        # stays quiet without the detail being unreachable.
        self.price_notice = QPushButton("")
        self.price_notice.setObjectName("Danger")
        self.price_notice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.price_notice.setFlat(True)
        self.price_notice.setStyleSheet("text-align: left;")
        self.price_notice.setVisible(False)
        self.price_notice.clicked.connect(self._toggle_price_history)
        # A row rather than the bare notice, so the export button can sit at
        # the end of it. The notice keeps the full width it had: it is the
        # click target that opens the table, and shrinking it to its own text
        # would take most of that target away.
        price_row = QHBoxLayout()
        price_row.addWidget(self.price_notice, stretch=1)
        layout.addLayout(price_row)

        self.price_table = QTableWidget(0, len(_PRICE_HEADERS))
        self.price_table.setHorizontalHeaderLabels(_PRICE_HEADERS)
        self.price_table.verticalHeader().setVisible(False)
        self.price_table.setAlternatingRowColors(True)
        self.price_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.price_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        resizable_columns(self.price_table, ledger, "recurring.prices", stretch=0)
        # Held explicitly rather than read back off the widget. `isVisible()`
        # is False for any child whose parent has not been shown yet, so using
        # it as the source of truth makes the first toggle a no-op in any
        # context where the view is built before it is displayed.
        self._price_open = bool(ledger.setting("price_history_open"))
        self.price_table.setVisible(self._price_open)

        # Hidden whenever the table is. This history is collapsible, and
        # offering to export rows nobody can see is the opposite of what a
        # button labelled "as it is shown" promises.
        self.price_export = export_button(self.price_table, self, "price history")
        self.price_export.setVisible(self._price_open)
        price_row.addWidget(self.price_export)

        # Bills, subscriptions and stopped things answer different questions,
        # so they get their own tabs rather than one list the user must scan.
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)
        tab_row = QHBoxLayout()
        self.tabs = FilterStrip()
        self.tabs.setReorderable(True)
        for label in self._tab_order():
            self.tabs.addTab(label)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.tabs.orderChanged.connect(self._save_tab_order)
        # The strip takes the spare width itself. It used to sit beside an
        # addStretch, which claimed all the room, and a wrapping strip given
        # its minimum width wraps after every chip -- eight chips stacked one
        # per line, 111 pixels wide and 282 tall, taking the height the table
        # beneath it needed.
        tab_row.addWidget(self.tabs, stretch=1)

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
        main_layout.addLayout(tab_row)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        # Headers must sit over their columns: text left, numbers right.
        for column in range(len(_HEADERS)):
            align = (
                Qt.AlignmentFlag.AlignLeft
                if column < _FIRST_NUMERIC_COLUMN
                else Qt.AlignmentFlag.AlignRight
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
        resizable_columns(self.table, ledger, "recurring", stretch=0)
        main_layout.addWidget(self.table, stretch=1)

        # A splitter between the price history and the main table, so the two
        # can share the height however the moment wants -- the same control
        # Net worth has. The chips stay on the table's side of the divider,
        # since they are what filter it.
        #
        # Added directly rather than through `add_panel`: the price notice
        # above is already that panel's heading and its open/close control,
        # and a second header with a title and a maximise button would be
        # furniture on top of it. While the history is collapsed the price
        # table is hidden, which hides its handle too, so the table below
        # simply has the room.
        self.panels = PanelSplitter("recurring", ledger, defaults=(1, 3))
        self.panels.addWidget(self.price_table)
        self.panels.addWidget(main)
        self.panels.setStretchFactor(0, 1)
        self.panels.setStretchFactor(1, 3)
        self.panels.setCollapsible(1, False)
        layout.addWidget(self.panels, stretch=1)

        # Into the tab row above, now that there is a table to point it at.
        # What comes out is what the chips and the search have left showing.
        tab_row.addWidget(export_button(self.table, self, "recurring"))

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

        self.panels.restore()
        self.refresh()

    def _apply_search(self) -> None:
        """Hide rows the search text does not appear in.

        Rows are hidden rather than the table rebuilt, so sorting survives and
        the running totals keep describing the tab rather than the search.
        Matched across every text column at once, since typing "yearly",
        "bill" or "wells fargo" is as natural as typing a merchant name.
        """
        needle = self.search.text().strip().lower()
        hidden = 0
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            cells = (self.table.item(row, column) for column in _SEARCHABLE_COLUMNS)
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

    # The two catch-alls sit at the end: they are where you go when the
    # specific tabs did not have it, which is the opposite of a default.
    DEFAULT_TABS = (
        "Subscriptions",
        "Bills",
        "Income",
        "Habits",
        "Cancelled",
        "Stopped",
        "Hidden",
        "All",
    )

    def _tab_order(self) -> list[str]:
        """The user's order, reconciled with the tabs that actually exist.

        Saved orders outlive the code that wrote them. Anything the saved
        list no longer knows about is appended rather than dropped, and
        anything it names that no longer exists is ignored -- so adding or
        removing a tab in a later version cannot strand the user with a
        strip that is missing one.
        """
        saved = self.ledger.setting("subscriptions_tab_order") or []
        if not isinstance(saved, list):
            return list(self.DEFAULT_TABS)
        known = [str(label) for label in saved if str(label) in self.DEFAULT_TABS]
        missing = [label for label in self.DEFAULT_TABS if label not in known]
        return known + missing

    def _save_tab_order(self, labels: list) -> None:
        self.ledger.save_setting("subscriptions_tab_order", [str(x) for x in labels])

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
            # Only while it is still news. A marker that never expires stops
            # being read: the row wears an arrow for ever and says nothing
            # about what the thing costs today. The change itself is kept --
            # it moves to the price history panel above, which is where the
            # long view belongs.
            if change is not None and change.is_recent():
                name += "  ↑" if change.direction == "increase" else "  ↓"
            if self.ledger.is_edited(item):
                name += "  ✎"
            # Show what the series costs now rather than its historical median,
            # which is stale the moment a price changes.
            amount = self.ledger.current_amount(item)
            annual = self.ledger.current_annual(item)
            paid_with = self.ledger.paid_with(item)
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
                # Blank when nothing is known, and sorted to the bottom
                # rather than the top: an unanswered question is not a name
                # that happens to start with a space.
                SortableItem(paid_with or "—", (paid_with or "\uffff").lower()),
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
                if column >= _FIRST_NUMERIC_COLUMN:
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
        self._refresh_price_history()

        self.footnote.setText("   ·   ".join(notes))
        pending = len(unknown)
        self.review_button.setText(
            f"Classify {pending} unclassified…" if pending else "Reclassify a merchant…"
        )
        self.review_button.setEnabled(bool(series))

    # -- what things used to cost ------------------------------------------

    def _toggle_price_history(self) -> None:
        self._price_open = not self._price_open
        self.ledger.save_setting("price_history_open", self._price_open)
        self._refresh_price_history()
        if self._price_open:
            self._give_price_history_room()

    def _give_price_history_room(self) -> None:
        """Open the history at a usable height, not at the zero it closed at.

        A splitter remembers a hidden panel's size as nothing, and showing
        the panel again does not change that -- so opening the history
        produced a heading, an open caret, and a table zero pixels tall. A
        quarter of the space unless the user has already dragged it to
        something else.
        """
        sizes = self.panels.sizes()
        if len(sizes) == 2 and sizes[0] == 0:
            total = sum(sizes) or 800
            share = max(total // 4, 160)
            self.panels.setSizes([share, max(total - share, 0)])
            self.panels.save()

    def _refresh_price_history(self) -> None:
        changes = sorted(
            self.ledger.price_changes, key=lambda c: c.changed_on, reverse=True
        )
        if not changes:
            self.price_notice.setVisible(False)
            self.price_table.setVisible(False)
            self.price_export.setVisible(False)
            return

        rises = [c for c in changes if c.direction == "increase"]
        recent = [c for c in changes if c.is_recent()]
        count = len(changes)
        plural = "" if count == 1 else "s"
        caret = "▾" if self._price_open else "▸"

        if recent:
            biggest = max(recent, key=lambda c: abs(c.annual_impact.minor))
            arrow = "↑" if biggest.direction == "increase" else "↓"
            others = len(recent) - 1
            more = f" — and {others} other{'' if others == 1 else 's'} changed too"
            headline = (
                f"{arrow} {biggest.merchant} went from "
                f"{abs(biggest.old_amount).format()} to "
                f"{abs(biggest.new_amount).format()} on {biggest.changed_on}"
                f"{more if others else ''}."
            )
        elif rises:
            # Nothing lately, so this no longer leads with a merchant and a
            # date as though it had just happened. What a past rise still
            # costs every year is the part that stays true.
            extra = total([abs(c.annual_impact) for c in rises])
            headline = f"Past price rises are costing you {extra.format()}/year more."
        else:
            headline = f"{count} price change{plural} on record."

        self.price_notice.setText(f"{headline}  {caret} price history")
        self.price_notice.setVisible(True)
        self.price_table.setVisible(self._price_open)
        self.price_export.setVisible(self._price_open)

        self.price_table.setRowCount(len(changes))
        for row, change in enumerate(changes):
            per_year = change.annual_impact
            cells = [
                SortableItem(change.merchant, change.merchant.lower()),
                SortableItem(abs(change.old_amount).format(), abs(change.old_amount).minor),
                SortableItem(abs(change.new_amount).format(), abs(change.new_amount).minor),
                SortableItem(change.changed_on.isoformat(), change.changed_on.toordinal()),
                SortableItem(change.cadence, change.cadence),
                SortableItem(
                    ("+" if per_year.minor < 0 else "-") + abs(per_year).format(),
                    -per_year.minor,
                ),
            ]
            for column, cell in enumerate(cells):
                if column:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
            # Red for a rise, green for a cut, on the two columns that carry
            # the direction.
            tone = theme.ACTIVE.danger if change.direction == "increase" else theme.ACTIVE.accent
            cells[2].setForeground(QColor(tone))
            cells[5].setForeground(QColor(tone))
            if not change.is_recent():
                cells[3].setForeground(QColor(theme.ACTIVE.muted))
            for column, cell in enumerate(cells):
                self.price_table.setItem(row, column, cell)

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

        # Anything can say what pays for it. For a detected series the account
        # the charge landed in is a fact, but who settles it is a separate
        # question the statement cannot answer, so the answer is stored as a
        # correction over the top rather than replacing what was observed.
        route = QAction("Paid with…", self)
        route.setToolTip("Which card, account or person this actually bills to.")
        route.triggered.connect(lambda: self._set_paid_with(series))
        menu.addAction(route)

        if self.ledger.paid_with_is_corrected(series):
            revert = QAction("Use what the statement says", self)
            revert.setToolTip("Drop your correction and go back to the account it landed in.")
            revert.triggered.connect(lambda: self._clear_paid_with(series))
            menu.addAction(revert)

        if self.ledger.is_manual(series):
            # A tracked entry has no charges to count forward from, so the
            # next one can only be projected from a date the user gives.
            when = QAction("Last billed on…", self)
            when.setToolTip("Sets the date the next charge is counted forward from.")
            when.triggered.connect(lambda: self._set_billed_on(series))
            menu.addAction(when)

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

    def _set_paid_with(self, series) -> None:
        """Change which card, account or person a subscription bills to."""
        entry = self.ledger.manual_entry(series) if self.ledger.is_manual(series) else None
        if entry is not None:
            text = str(entry.get("paid_via") or "")
            account = str(entry.get("paid_via_account") or "")
        else:
            # A detected series starts from whatever correction is already in
            # place, or from the account the charge landed in.
            fields = self.ledger.overrides.get(series.merchant.upper(), {})
            text = str(fields.get("paid_via") or "")
            account = str(fields.get("paid_via_account") or series.account_id or "")

        choice = paid_with.prompt(
            series.merchant,
            self.ledger.payable_accounts,
            text,
            account,
            self,
        )
        if choice is None:
            return
        self.ledger.set_paid_with(series, choice)
        refresh_everything(self)

    def _set_billed_on(self, series) -> None:
        """Give a tracked entry the date its next charge counts forward from."""
        current = self.ledger.billed_on(series)
        chosen = billing_date.prompt(
            series.merchant,
            str(series.cadence),
            current,
            self,
            suggested=self.ledger.suggest_billed_on(series) if current is None else None,
        )
        # None is an answer ("I do not know, blank it"), so cancelling needs
        # its own value rather than sharing one with a real choice.
        if chosen is billing_date.CANCELLED:
            return
        self.ledger.set_billed_on(series, chosen)
        refresh_everything(self)

    def _clear_paid_with(self, series) -> None:
        """Drop the user's correction and show the statement's answer again."""
        self.ledger.clear_paid_with(series)
        refresh_everything(self)

    def _add_manual(self) -> None:
        """Record a subscription no detector can see."""
        values = add_subscription.prompt(self.ledger.payable_accounts, self)
        if values is None:
            return
        self.ledger.add_manual(values)
        refresh_everything(self)

    def _edit(self, series) -> None:
        corrections = edit_series.prompt(series, self.ledger.is_edited(series), self)
        if corrections is None:
            return
        if corrections == {}:
            self.ledger.reset_series(series)
        else:
            self.ledger.edit_series(series, **corrections)
        refresh_everything(self)

    def _dismiss(self, series) -> None:
        """This does not recur. Drop it from every screen, Upcoming included."""
        self.ledger.dismiss(series)
        refresh_everything(self)

    def _restore(self, series) -> None:
        self.ledger.restore(series)
        self.ledger.load()
        refresh_everything(self)

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
            refresh_everything(self)

    def _remove_manual(self, series) -> None:
        if self.ledger.remove_manual(series):
            refresh_everything(self)

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

    def _classify(self, series, *, redraw: bool = True) -> bool:
        """Ask about one merchant. Returns False if the user cancelled.

        `redraw` exists for the review loop, which asks about many merchants
        in a row: rebuilding every screen between two modal dialogs is work
        nobody sees, so the loop refreshes once at the end instead.
        """
        dialog = ClassifyDialog(series, self.ledger.kind_of(series), self)
        if dialog.exec() != ClassifyDialog.DialogCode.Accepted:
            return False
        self.ledger.set_kind(series, dialog.chosen)
        if redraw:
            refresh_everything(self)
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
            if not self._classify(series, redraw=False):
                break
        refresh_everything(self)
