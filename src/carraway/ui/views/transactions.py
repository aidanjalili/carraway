"""The transaction list.

Backed by a model rather than a widget-per-cell table: a real ledger runs to
tens of thousands of rows, and QTableWidget builds an object for every cell of
every one of them. The model builds only what is on screen.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...analysis import categorize as cat
from ...core.models import Transaction
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import BalanceBanner, FilterStrip, enable_row_hover, refresh_everything, shorten
from . import cash

# Ranges someone actually asks for, with None meaning "everything".
_PRESETS: dict[str, int | None] = {
    "All time": None,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Last 6 months": 182,
    "Last year": 365,
    "Custom": None,
}

_COLUMNS = ["Date", "Description", "Category", "Account", "Amount"]

# Qt overrides need a QModelIndex default; one shared invalid index avoids
# constructing a throwaway on every call.
_NO_PARENT = QModelIndex()


class TransactionModel(QAbstractTableModel):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self.rows: list[Transaction] = list(ledger.transactions)

    def rowCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        tx = self.rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return [
                tx.date.isoformat(),
                tx.description,
                self._category_label(tx),
                self.ledger.account_name(tx.account_id),
                tx.amount.format(),
            ][column]

        # Sorting must compare real values, not formatted strings, or amounts
        # order lexicographically and dates only work by accident.
        if role == Qt.ItemDataRole.UserRole:
            return [
                tx.date.toordinal(),
                tx.description.lower(),
                self.ledger.categories.get(tx.id, ""),
                self.ledger.account_name(tx.account_id),
                tx.amount.minor,
            ][column]

        if role == Qt.ItemDataRole.TextAlignmentRole and column == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Qt's darkGreen is unreadable on a dark background, so take the
        # accent from whichever palette is actually running.
        if role == Qt.ItemDataRole.ForegroundRole and column == 4 and not tx.is_outflow:
            return QColor(theme.ACTIVE.accent)
        # A guessed category is drawn muted, so the eye can skip the ones that
        # are only probably right.
        if role == Qt.ItemDataRole.ForegroundRole and column == 2 and self.ledger.is_guessed(tx.id):
            return QColor(theme.ACTIVE.warning)
        if role == Qt.ItemDataRole.ToolTipRole and column == 2:
            reason = self.ledger.guess_reason(tx.id)
            return f"Guessed: {reason}" if reason else None
        return None

    def _category_label(self, tx: Transaction) -> str:
        if tx.is_transfer:
            return "Transfer"
        name = self.ledger.categories.get(tx.id, "")
        # The "?" is the whole point of guessing: a guess that looks like a
        # certainty is worse than none.
        return f"{name}  ?" if self.ledger.is_guessed(tx.id) else name

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        # The amount header has to sit over its right-aligned column.
        if role == Qt.ItemDataRole.TextAlignmentRole and section == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class _FilterProxy(QSortFilterProxyModel):
    """Search across description, category and account at once.

    Qt's default filter looks at one column, which is never what someone means
    when they type "netflix" into a search box.

    Also narrows to a single account, so the account tabs and the search box
    compose rather than overriding each other: searching inside one account is
    the obvious thing to want and would be impossible if either reset the
    other.
    """

    def __init__(self) -> None:
        super().__init__()
        self.account_id: str | None = None
        self.kind: str = "All"
        self.since: date | None = None
        self.until: date | None = None

    def set_account(self, account_id: str | None) -> None:
        self.account_id = account_id
        self.invalidateFilter()

    def set_range(self, since: date | None, until: date | None) -> None:
        self.since = since
        self.until = until
        self.invalidateFilter()

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if self.account_id is not None:
            transaction = model.rows[row]
            if transaction.account_id != self.account_id:
                return False

        if self.since or self.until:
            when = model.rows[row].date
            if self.since and when < self.since:
                return False
            if self.until and when > self.until:
                return False

        if self.kind != "All":
            transaction = model.rows[row]
            category = model.ledger.categories.get(transaction.id, "")
            if self.kind == "Spending":
                if not transaction.is_outflow or transaction.is_transfer:
                    return False
            elif self.kind == "Income":
                if transaction.is_outflow or transaction.is_transfer:
                    return False
            elif self.kind == "Transfers":
                if not (transaction.is_transfer or category == "Transfer"):
                    return False
            elif self.kind == "Pending":
                if not transaction.pending:
                    return False
            elif self.kind != category:
                return False

        needle = self.filterRegularExpression().pattern().lower()
        if not needle:
            return True
        for column in (1, 2, 3):
            value = model.data(model.index(row, column, parent), Qt.ItemDataRole.DisplayRole)
            if needle in str(value).lower():
                return True
        return False


class TransactionsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Transactions")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        # One button per account, with "All accounts" first. Accounts carry
        # very different volumes, so the count sits in the label: it is the
        # fastest way to see which account a run of transactions came from.
        # A wrapping row rather than a tab bar, because ten accounts in a tab
        # bar hides most of them behind scroll arrows.
        self.tabs = FilterStrip()
        self.tabs.currentChanged.connect(self._account_changed)
        layout.addWidget(self.tabs)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search description, category or account…")
        self.search.setClearButtonEnabled(True)
        search_row.addWidget(self.search, stretch=1)

        # Direction first, then categories: "show me only income" is asked far
        # more often than "show me only Pets", and a single list keeps the
        # control simple.
        self.kind = QComboBox()
        self.kind.addItems(["All", "Spending", "Income", "Transfers", "Pending"])
        self.kind.insertSeparator(self.kind.count())
        # From the ledger rather than the built-in list, so a category the
        # user added is filterable and one they hid is not.
        self.kind.addItems(list(ledger.categories_available or cat.CATEGORIES))
        self.kind.currentTextChanged.connect(self._kind_changed)
        search_row.addWidget(QLabel("Type"))
        search_row.addWidget(self.kind)
        layout.addLayout(search_row)

        range_row = QHBoxLayout()
        range_row.setSpacing(8)
        range_row.addWidget(QLabel("Dates"))

        # Named ranges first, because "last 3 months" is what someone actually
        # wants nine times in ten; the exact dates are there for the tenth.
        self.range_preset = QComboBox()
        self.range_preset.addItems(list(_PRESETS))
        self.range_preset.currentTextChanged.connect(self._preset_chosen)
        range_row.addWidget(self.range_preset)

        self.since = QDateEdit()
        self.since.setCalendarPopup(True)
        self.since.setDisplayFormat("yyyy-MM-dd")
        self.since.dateChanged.connect(lambda _: self._dates_edited())
        range_row.addWidget(self.since)

        self.dates_to_label = QLabel("to")
        range_row.addWidget(self.dates_to_label)
        self.until = QDateEdit()
        self.until.setCalendarPopup(True)
        self.until.setDisplayFormat("yyyy-MM-dd")
        self.until.dateChanged.connect(lambda _: self._dates_edited())
        range_row.addWidget(self.until)

        # The view opens on "All time", and `_preset_chosen` only runs when
        # the box changes -- so without this the pickers started enabled under
        # a preset that leaves them nothing to say.
        self._set_dates_enabled(self.range_preset.currentText() != "All time")

        range_row.addStretch(1)
        export = QPushButton("Export this view…")
        export.setCursor(Qt.CursorShape.PointingHandCursor)
        export.setToolTip(
            "Writes exactly the rows shown, with the account, type, date range "
            "and search all applied."
        )
        export.clicked.connect(self._export_view)
        range_row.addWidget(export)
        layout.addLayout(range_row)

        self.model = TransactionModel(ledger)
        self.proxy = _FilterProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(self._update_count)

        # Above the table rather than beside the title: it describes what the
        # table is showing, and it stays put while the rows scroll.
        self.balance = BalanceBanner()
        # Cash is the only account whose balance no feed can report, so it is
        # the only one where typing a figure is meaningful rather than
        # something the next sync would quietly overwrite.
        self.set_balance_button = QPushButton("Set balance…")
        self.set_balance_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_balance_button.setToolTip(
            "Type what is actually there. Carraway records it as today's balance."
        )
        self.set_balance_button.clicked.connect(self._set_cash_balance)
        self.add_txn_button = QPushButton("Add transaction…")
        self.add_txn_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_txn_button.setToolTip("Record a cash movement by hand.")
        self.add_txn_button.clicked.connect(self._add_cash_transaction)
        self.balance.add_action(self.add_txn_button)
        self.balance.add_action(self.set_balance_button)
        layout.addWidget(self.balance)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # Row-wide hover; Qt's stylesheet :hover only covers one cell.
        self._hover = enable_row_hover(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.count = QLabel("")
        self.count.setObjectName("Muted")
        layout.addWidget(self.count)

        self._build_tabs()
        self._reset_dates()
        # The banner was built empty and only filled in when a tab was
        # clicked, so opening Transactions showed a blank card where the
        # balance goes -- and the two cash buttons visible over it, which
        # belong to a cash account rather than to "all accounts".
        self._update_balance(self.tabs.tabData(self.tabs.currentIndex()))
        self._update_count()

    def _reset_dates(self) -> None:
        """Set the pickers to the ledger's own span, without filtering yet."""
        dates = [t.date for t in self.ledger.transactions]
        first, last = (min(dates), max(dates)) if dates else (date.today(), date.today())
        for picker, value in ((self.since, first), (self.until, last)):
            picker.blockSignals(True)
            picker.setDateRange(
                QDate(first.year, first.month, first.day), QDate(last.year, last.month, last.day)
            )
            picker.setDate(QDate(value.year, value.month, value.day))
            picker.blockSignals(False)

    def _preset_chosen(self, name: str) -> None:
        # "All time" is every date there is, so the two pickers have nothing
        # to say. Left enabled they invited an edit that immediately
        # contradicted the preset above them -- and the screenshot that
        # prompted this had "All time" selected beside a two-year window,
        # which is two answers to one question.
        self._set_dates_enabled(name != "All time")

        days = _PRESETS[name]
        dates = [t.date for t in self.ledger.transactions]
        last = max(dates) if dates else date.today()
        first = min(dates) if dates else date.today()
        since = first if days is None else max(first, last - timedelta(days=days))

        for picker, value in ((self.since, since), (self.until, last)):
            picker.blockSignals(True)
            picker.setDate(QDate(value.year, value.month, value.day))
            picker.blockSignals(False)
        self._apply_range(since, last)

    def _set_dates_enabled(self, enabled: bool) -> None:
        """Grey the pickers out when the preset already decides the range."""
        for picker in (self.since, self.until):
            picker.setEnabled(enabled)
            picker.setToolTip(
                "" if enabled else "Every date is included. Pick another range to set these."
            )
        self.dates_to_label.setEnabled(enabled)

    def _dates_edited(self) -> None:
        """A hand-picked date means the preset no longer describes the range."""
        self.range_preset.blockSignals(True)
        self.range_preset.setCurrentText("Custom")
        self.range_preset.blockSignals(False)
        since = self.since.date().toPython()
        until = self.until.date().toPython()
        self._apply_range(since, until)

    def _apply_range(self, since: date, until: date) -> None:
        dates = [t.date for t in self.ledger.transactions]
        widest = (min(dates), max(dates)) if dates else (since, until)
        # Passing None when the range covers everything keeps the filter out
        # of the hot path for the common case.
        self.proxy.set_range(
            None if since <= widest[0] else since,
            None if until >= widest[1] else until,
        )
        self._update_count()

    def _visible_transactions(self) -> list[Transaction]:
        """The rows on screen, in the order they are shown."""
        out = []
        for row in range(self.proxy.rowCount()):
            source = self.proxy.mapToSource(self.proxy.index(row, 0))
            out.append(self.model.rows[source.row()])
        return out

    def _export_view(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from ...exporters.ods import export_csv, export_ods

        rows = self._visible_transactions()
        if not rows:
            QMessageBox.information(self, "Nothing to export", "No rows match these filters.")
            return

        default = str(Path.home() / "carraway-selection.ods")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export these transactions", default, "Spreadsheet (*.ods);;CSV (*.csv)"
        )
        if not chosen:
            return

        target = Path(chosen)
        categories = [self.ledger.categories.get(t.id, "Uncategorized") for t in rows]
        try:
            if target.suffix.lower() == ".csv":
                written = export_csv(target, rows, categories=categories)
            else:
                # No balances, so no Net Worth sheet: that describes every
                # account, and this file is meant to be the rows on screen.
                # Accounts are still passed, to turn ids into names.
                written = export_ods(
                    target if target.suffix else target.with_suffix(".ods"),
                    rows,
                    accounts=self.ledger.accounts,
                    categories=categories,
                )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self, "Exported", f"{len(rows):,} transaction(s) written to\n{written}"
        )

    def _kind_changed(self, kind: str) -> None:
        self.proxy.set_kind(kind)
        self._update_count()

    def _build_tabs(self) -> None:
        """Rebuild the tab bar from the ledger's accounts."""
        # Signals off while rebuilding: removing tabs fires currentChanged and
        # would filter to whichever account happens to be left mid-rebuild.
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)

        counts: dict[str, int] = {}
        for transaction in self.ledger.transactions:
            counts[transaction.account_id] = counts.get(transaction.account_id, 0) + 1

        self.tabs.addTab(f"All accounts  ({len(self.ledger.transactions):,})")
        self.tabs.setTabData(0, None)
        # Busiest first: an account with three transactions is rarely the one
        # someone opened this screen to look at.
        for account in sorted(self.ledger.accounts, key=lambda a: -counts.get(a.id, 0)):
            label = f"{shorten(account.name, 26)}  ({counts.get(account.id, 0):,})"
            index = self.tabs.addTab(label)
            self.tabs.setTabData(index, account.id)
            self.tabs.setTabToolTip(
                index, f"{account.name} — {account.institution or account.type}"
            )

        self.tabs.blockSignals(False)

    def _account_changed(self, index: int) -> None:
        account_id = self.tabs.tabData(index)
        self.proxy.set_account(account_id)
        # The account column says the same thing as the tab once one is
        # chosen, so it only earns its place on "All accounts".
        self.table.setColumnHidden(3, account_id is not None)
        self._update_balance(account_id)
        self._update_count()

    def _update_balance(self, account_id: str | None) -> None:
        """Show the balance for whichever account the tabs are filtered to."""
        is_cash = self.ledger.is_cash_account(account_id)
        self.set_balance_button.setVisible(is_cash)
        self.add_txn_button.setVisible(is_cash)
        balances = self.ledger.balances
        if account_id is None:
            if not balances:
                self.balance.show_nothing("no balances recorded yet")
                return
            # Liabilities subtract: a card you owe $500 on is -$500 against
            # what you hold, which is what makes this figure a net worth
            # rather than a sum of unrelated numbers.
            net = sum(
                (
                    -abs(balance) if self._is_liability(aid) else balance
                    for aid, balance in balances.items()
                ),
                Money.zero(),
            )
            counted = len(balances)
            self.balance.show_balance(
                net.format(),
                f"net across {counted} account{'s' if counted != 1 else ''}",
                owed=net.minor < 0,
            )
            return

        name = self.ledger.account_name(account_id)
        balance = balances.get(account_id)
        if balance is None:
            self.balance.show_nothing(f"{name} · no balance recorded")
            return

        owed = self._is_liability(account_id) and balance.minor != 0
        self.balance.show_balance(
            abs(balance).format(),
            f"{name} · {'owed' if owed else 'balance'}",
            owed=owed,
        )

    def _cash_account(self) -> str | None:
        """The selected account id when it is a cash account, else None."""
        account_id = self.tabs.tabData(self.tabs.currentIndex())
        return account_id if self.ledger.is_cash_account(account_id) else None

    def _set_cash_balance(self) -> None:
        """Ask what the account really holds, and optionally reconcile to it."""
        account_id = self._cash_account()
        if account_id is None:
            return
        name = self.ledger.account_name(account_id)
        answer = cash.ask_balance(name, self.ledger.implied_balance(account_id), self)
        if answer is None:
            return
        gap = self.ledger.set_cash_balance(
            account_id, answer["amount"], correction=answer["correction"]
        )
        refresh_everything(self)
        if answer["correction"] and gap.minor:
            self.count.setText(
                f"Recorded {answer['amount'].format()} and added a "
                f"{abs(gap).format()} adjustment line."
            )

    def _add_cash_transaction(self) -> None:
        """Record a cash movement the user knows about."""
        account_id = self._cash_account()
        if account_id is None:
            return
        name = self.ledger.account_name(account_id)
        answer = cash.ask_transaction(name, self)
        if answer is None:
            return
        added = self.ledger.add_cash_transaction(
            account_id, answer["when"], answer["description"], answer["amount"]
        )
        refresh_everything(self)
        if not added:
            self.count.setText("That transaction was already recorded.")

    def _is_liability(self, account_id: str) -> bool:
        for account in self.ledger.accounts:
            if account.id == account_id:
                return account.type.is_liability
        return False

    def _update_count(self) -> None:
        shown, held = self.proxy.rowCount(), self.model.rowCount()
        self.count.setText(
            f"{shown:,} of {held:,} transactions" if shown != held else f"{held:,} transactions"
        )

    def _rebuild_kinds(self) -> None:
        """Re-fill the type filter after the category list changed."""
        chosen = self.kind.currentText()
        self.kind.blockSignals(True)
        self.kind.clear()
        self.kind.addItems(["All", "Spending", "Income", "Transfers", "Pending"])
        self.kind.insertSeparator(self.kind.count())
        self.kind.addItems(list(self.ledger.categories_available or cat.CATEGORIES))
        index = self.kind.findText(chosen)
        self.kind.setCurrentIndex(index if index >= 0 else 0)
        self.kind.blockSignals(False)
        if index < 0:
            # The chosen category no longer exists, so the filter it applied
            # must go too rather than silently showing nothing.
            self.proxy.set_kind("All")

    def refresh(self) -> None:
        self._rebuild_kinds()
        self.model.beginResetModel()
        self.model.rows = list(self.ledger.transactions)
        self.model.endResetModel()
        self._build_tabs()
        self._update_balance(self.tabs.tabData(self.tabs.currentIndex()))
        self._update_count()
