"""The application shell: a sidebar of screens and the screen itself."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import db
from . import sync_worker
from .assets import app_icon
from .data import Ledger
from .sync_worker import SyncRunner
from .views.budget_detail import BudgetDetailView
from .views.create_budget import CreateBudgetView
from .views.dashboard import DashboardView
from .views.networth import NetWorthView
from .views.settings import SettingsView
from .views.spending import SpendingView
from .views.subscriptions import SubscriptionsView
from .views.transactions import TransactionsView
from .views.upcoming import UpcomingView

# Net worth first: it is the one number that answers "how am I doing", and it
# is what someone opening the app wants before any breakdown of it.
_SCREENS = [
    ("Net worth", NetWorthView),
    ("Upcoming", UpcomingView),
    ("Subscriptions", SubscriptionsView),
    ("Create a budget", CreateBudgetView),
    ("Spending", SpendingView),
    ("Overview", DashboardView),
    ("Transactions", TransactionsView),
    ("Settings", SettingsView),
]


class MainWindow(QMainWindow):
    def __init__(self, database: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Carraway")
        self.resize(1180, 760)

        self.ledger = Ledger(path=database or db.default_db_path())
        self.ledger.load()

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        for _, factory in _SCREENS:
            self.stack.addWidget(factory(self.ledger))
        root_layout.addWidget(self.stack, stretch=1)

        self.setCentralWidget(root)
        self._rebuild_budget_nav()

        self.syncer = SyncRunner(self.ledger.path, self)
        self.syncer.started.connect(self._sync_started)
        self.syncer.finished.connect(self._sync_finished)
        self.syncer.failed.connect(self._sync_failed)
        self._describe_last_sync()

        # Kick off shortly after the window is up rather than during
        # construction, so the app is on screen and usable while it runs.
        QTimer.singleShot(600, self._sync_if_due)

        # Next charge dates are counted forward from today, and today is read
        # when the ledger loads. An app left open across midnight would go on
        # showing yesterday's answer -- and for anything billing today, a date
        # that has already passed. Cheap to check, and it only does anything
        # on the one tick a day when the date has actually changed.
        self._today = date.today()
        self._day_watch = QTimer(self)
        self._day_watch.setInterval(10 * 60 * 1000)  # ten minutes
        self._day_watch.timeout.connect(self._check_the_date)
        self._day_watch.start()

    def _check_the_date(self) -> None:
        """Reload if the calendar has moved on since the last check."""
        today = date.today()
        if today == self._today:
            return
        self._today = today
        self.ledger.load()
        self.refresh_all()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 22, 14, 18)
        layout.setSpacing(4)

        # The mark beside the name, so the sidebar matches the taskbar.
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 8, 0)
        brand.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(26, 26))
        brand.addWidget(mark)

        wordmark = QLabel("Carraway")
        wordmark.setStyleSheet("font-size: 19px; font-weight: 700;")
        tagline = QLabel("your money, locally")
        tagline.setObjectName("Muted")
        tagline.setStyleSheet("padding: 0 8px 18px 8px; font-size: 12px;")
        brand.addWidget(wordmark)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addWidget(tagline)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (name, _) in enumerate(_SCREENS):
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)

        # Saved budgets get their own section, rebuilt whenever one is added
        # or removed. Held in a layout of its own so rebuilding it does not
        # disturb the fixed screens above or the buttons below.
        self.budgets_heading = QLabel("MY BUDGETS")
        self.budgets_heading.setObjectName("StatLabel")
        self.budgets_heading.setStyleSheet("padding: 14px 8px 4px 8px;")
        layout.addWidget(self.budgets_heading)

        self.budgets_nav = QVBoxLayout()
        self.budgets_nav.setContentsMargins(0, 0, 0, 0)
        self.budgets_nav.setSpacing(4)
        layout.addLayout(self.budgets_nav)

        self.no_budgets = QLabel("None yet")
        self.no_budgets.setObjectName("Muted")
        self.no_budgets.setStyleSheet("padding: 2px 8px 0 8px; font-size: 12px;")
        layout.addWidget(self.no_budgets)

        layout.addStretch(1)

        self.refresh_button = QPushButton("Refresh from banks")
        self.refresh_button.setObjectName("NavButton")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self._sync_now)
        layout.addWidget(self.refresh_button)

        self.sync_status = QLabel("")
        self.sync_status.setObjectName("Muted")
        self.sync_status.setStyleSheet("padding: 0 8px; font-size: 11px;")
        self.sync_status.setWordWrap(True)
        layout.addWidget(self.sync_status)

        import_button = QPushButton("Import statements…")
        import_button.setObjectName("NavButton")
        import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        import_button.clicked.connect(self._import)
        layout.addWidget(import_button)

        export = QPushButton("Export to Calc…")
        export.setObjectName("NavButton")
        export.setCursor(Qt.CursorShape.PointingHandCursor)
        export.clicked.connect(self._export)
        layout.addWidget(export)

        accounts = QLabel(f"{len(self.ledger.accounts)} accounts")
        accounts.setObjectName("Muted")
        accounts.setStyleSheet("padding: 0 8px; font-size: 12px;")
        privacy = QLabel("Nothing leaves this device.")
        privacy.setObjectName("Muted")
        privacy.setStyleSheet("padding: 4px 8px 0 8px; font-size: 11px;")
        layout.addWidget(accounts)
        layout.addWidget(privacy)
        return sidebar

    # -- syncing ---------------------------------------------------------

    def _sync_if_due(self) -> None:
        """Sync on open, but only when the data is actually stale."""
        if not sync_worker.is_configured():
            self.sync_status.setText("No bank connected.")
            return
        conn = db.connect(self.ledger.path)
        due = sync_worker.is_due(conn)
        conn.close()
        if due:
            self.syncer.start()
        else:
            self._describe_last_sync()

    def _sync_now(self) -> None:
        """The Refresh button.

        Refused rather than queued when a limit says no. SimpleFIN allows 24
        requests a day and one sync spends about six, so a button with no
        limit could drain a day's quota in under a minute — including the
        share the scheduled sync depends on.
        """
        if not sync_worker.is_configured():
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self,
                "No bank connected",
                "Connect one first with:\n\n    carraway simplefin setup",
            )
            return

        conn = db.connect(self.ledger.path)
        refusal = sync_worker.refusal_reason(conn)
        conn.close()
        if refusal:
            self.sync_status.setText(refusal)
            return

        if not self.syncer.start():
            self.sync_status.setText("Already refreshing…")

    def _sync_started(self) -> None:
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Refreshing…")
        self.sync_status.setText("Fetching from your banks…")

    def _sync_finished(self, inserted: int, skipped: int, warnings: list) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh from banks")
        # Only reload when something actually changed; rebuilding every screen
        # to show the same numbers is a visible stutter for no reason.
        if inserted:
            self.ledger.load()
            self.refresh_all()
        note = f"{inserted} new transaction(s)" if inserted else "Up to date"
        conn = db.connect(self.ledger.path)
        left = sync_worker.requests_left(conn)
        conn.close()
        # Shown only when it starts to matter, so the normal case stays quiet.
        if left < 12:
            note += f" · {left} bank requests left today"
        if warnings:
            note += f" · {warnings[0][:50]}"
        self.sync_status.setText(note)

        # Freshly synced numbers are the ones worth having on the phone.
        # Quiet on failure: see publish_in_background.
        from .views.pocket import publish_in_background

        publish_in_background(self, self.ledger)

    def _sync_failed(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh from banks")
        # Reported in place rather than as a dialog: a sync failing while
        # someone reads their spending should not interrupt them.
        self.sync_status.setText(f"Refresh failed — {message[:70]}")

    def _describe_last_sync(self) -> None:
        conn = db.connect(self.ledger.path)
        previous = sync_worker.last_sync(conn)
        conn.close()
        if previous is None:
            self.sync_status.setText("Never refreshed.")
            return
        minutes = int((datetime.now() - previous).total_seconds() // 60)
        if minutes < 1:
            when = "just now"
        elif minutes < 60:
            when = f"{minutes} min ago"
        elif minutes < 60 * 24:
            when = f"{minutes // 60}h ago"
        else:
            when = f"{minutes // (60 * 24)}d ago"
        self.sync_status.setText(f"Last refreshed {when}")

    def _import(self) -> None:
        """Import one or more statement files, then reload everything."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from ..core.models import Account, AccountType
        from ..importers.csv_importer import ImportError_, import_csv
        from ..importers.ofx_importer import import_ofx
        from ..importers.venmo import import_venmo, looks_like_venmo, statement_balance

        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "Import statements",
            str(Path.home() / "Downloads"),
            "Statements (*.csv *.ofx *.qfx);;All files (*)",
        )
        if not chosen:
            return

        conn = db.connect(self.ledger.path)
        accounts = {a.id: a for a in db.list_accounts(conn)}
        lines: list[str] = []
        total_new = total_skipped = 0

        # Several files at once, because an export capped at 90 days means a
        # year of history arrives as a stack of them.
        for name in sorted(chosen):
            path = Path(name)
            suffix = path.suffix.lower()
            if suffix in (".ofx", ".qfx"):
                reader, account_id = import_ofx, None
            elif suffix == ".csv" and looks_like_venmo(path):
                reader = import_venmo
                existing = next(
                    (a for a in accounts.values() if a.institution.lower() == "venmo"), None
                )
                if existing is None:
                    existing = Account(
                        id=uuid.uuid4().hex[:12],
                        name="Venmo",
                        type=AccountType.CASH,
                        institution="Venmo",
                    )
                    db.upsert_account(conn, existing)
                    accounts[existing.id] = existing
                    lines.append(f"Created a Venmo account for {path.name}")
                account_id = existing.id
            else:
                reader, account_id = import_csv, None

            if account_id is None:
                account_id = self._ask_for_account(accounts, path)
                if account_id is None:
                    lines.append(f"{path.name}: skipped")
                    continue

            try:
                if reader is import_venmo:
                    transactions, _ = reader(path, account_id)
                else:
                    transactions, _ = reader(path, account_id)
            except ImportError_ as exc:
                lines.append(f"{path.name}: {exc}")
                continue

            if reader is import_venmo:
                # The statement's own closing balance is the only place a
                # payment-app balance appears, and recording it is what makes
                # the account countable towards net worth.
                closing = statement_balance(path)
                if closing is not None:
                    observed, amount = closing
                    db.record_balance(conn, account_id, amount, observed)

            inserted, skipped = db.insert_transactions(conn, transactions)
            total_new += inserted
            total_skipped += skipped
            lines.append(f"{path.name}: {inserted} new, {skipped} already present")

        conn.close()
        self.ledger.load()
        self.refresh_all()

        QMessageBox.information(
            self,
            "Import complete",
            f"{total_new} new transaction(s), {total_skipped} already present.\n\n"
            + "\n".join(lines[:12]),
        )

    def _ask_for_account(self, accounts: dict, path: Path) -> str | None:
        """Which account does this file belong to? Bank formats never say."""
        from PySide6.QtWidgets import QInputDialog

        if not accounts:
            return None
        labels = [f"{a.name} ({a.institution or a.type})" for a in accounts.values()]
        ids = list(accounts)
        choice, ok = QInputDialog.getItem(
            self, "Which account?", f"{path.name} belongs to:", labels, 0, False
        )
        return ids[labels.index(choice)] if ok else None

    def refresh_all(self) -> None:
        """Reload every screen after the ledger changes underneath them."""
        self._rebuild_budget_nav()
        for index in range(self.stack.count()):
            view = self.stack.widget(index)
            if hasattr(view, "refresh"):
                view.refresh()

    # -- saved budgets in the sidebar -------------------------------------

    def _rebuild_budget_nav(self) -> None:
        """Put one nav button and one screen behind every saved budget.

        Screens are torn down and rebuilt rather than diffed: there are a
        handful of budgets at most, and a stale screen pointing at a deleted
        budget is a whole class of bug not worth the saved microseconds.
        """
        remembered = self._current_budget_id()

        while self.budgets_nav.count():
            item = self.budgets_nav.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                self.nav_group.removeButton(widget)
                widget.deleteLater()
        for index in reversed(range(len(_SCREENS), self.stack.count())):
            view = self.stack.widget(index)
            self.stack.removeWidget(view)
            view.deleteLater()

        budgets = list(self.ledger.budgets)
        self.no_budgets.setVisible(not budgets)
        self._budget_screens: dict[str, int] = {}

        for budget in budgets:
            index = self.stack.addWidget(BudgetDetailView(self.ledger, budget.id))
            self._budget_screens[budget.id] = index
            button = QPushButton(budget.name[:22])
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(
                f"{budget.name} — {budget.starts_on.isoformat()} to "
                f"{budget.ends_on.isoformat()}, {budget.total.format()}"
            )
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            self.nav_group.addButton(button, index)
            self.budgets_nav.addWidget(button)

        # Stay where the user was, unless the budget they were looking at has
        # just been deleted — then fall back to the screen that makes them.
        if remembered and remembered in self._budget_screens:
            self.show_budget(remembered)
        elif remembered:
            self._show_screen("Create a budget")

    def _current_budget_id(self) -> str | None:
        view = self.stack.currentWidget()
        return getattr(view, "budget_id", None)

    def _show_screen(self, name: str) -> None:
        for index, (label, _) in enumerate(_SCREENS):
            if label == name:
                self.stack.setCurrentIndex(index)
                button = self.nav_group.button(index)
                if button is not None:
                    button.setChecked(True)
                return

    def show_budget(self, budget_id: str) -> None:
        """Open a budget's screen and check its button in the sidebar."""
        index = getattr(self, "_budget_screens", {}).get(budget_id)
        if index is None:
            return
        self.stack.setCurrentIndex(index)
        button = self.nav_group.button(index)
        if button is not None:
            button.setChecked(True)

    def _export(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from ..analysis import categorize as cat
        from ..exporters.ods import export_csv, export_ods

        default = str(Path.home() / "carraway.ods")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export for LibreOffice Calc", default, "Spreadsheet (*.ods);;CSV (*.csv)"
        )
        if not chosen:
            return

        target = Path(chosen)
        # Categories are computed rather than stored, so an export reading only
        # the saved column would file every row as Uncategorized.
        categories = cat.categorize_all(self.ledger.transactions)
        try:
            if target.suffix.lower() == ".csv":
                written = export_csv(target, self.ledger.transactions, categories=categories)
            else:
                kinds = {s.merchant.upper(): self.ledger.kind_of(s) for s in self.ledger.series}
                paid_via = {
                    str(t["merchant"]).upper(): str(t["paid_via"])
                    for t in self.ledger.manual
                    if t["paid_via"]
                }
                written = export_ods(
                    target.with_suffix(".ods") if not target.suffix else target,
                    self.ledger.transactions,
                    accounts=self.ledger.accounts,
                    series=self.ledger.series,
                    categories=categories,
                    balances=self.ledger.balances,
                    kinds=kinds,
                    paid_via=paid_via,
                )
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self,
            "Exported",
            f"{len(self.ledger.transactions):,} transactions written to\n{written}",
        )
