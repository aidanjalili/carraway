"""The application shell: a sidebar of screens and the screen itself."""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import Qt
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
from .data import Ledger
from .views.budget import BudgetView
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
    ("Budget", BudgetView),
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

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 22, 14, 18)
        layout.setSpacing(4)

        wordmark = QLabel("Carraway")
        wordmark.setStyleSheet("font-size: 19px; font-weight: 700; padding: 0 8px 4px 8px;")
        tagline = QLabel("your money, locally")
        tagline.setObjectName("Muted")
        tagline.setStyleSheet("padding: 0 8px 18px 8px; font-size: 12px;")
        layout.addWidget(wordmark)
        layout.addWidget(tagline)

        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (name, _) in enumerate(_SCREENS):
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            group.addButton(button, index)
            layout.addWidget(button)

        layout.addStretch(1)

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

    def _import(self) -> None:
        """Import one or more statement files, then reload everything."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from ..core.models import Account, AccountType
        from ..importers.csv_importer import ImportError_, import_csv
        from ..importers.ofx_importer import import_ofx
        from ..importers.venmo import import_venmo, looks_like_venmo

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
        for index in range(self.stack.count()):
            view = self.stack.widget(index)
            if hasattr(view, "refresh"):
                view.refresh()

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
                written = export_ods(
                    target.with_suffix(".ods") if not target.suffix else target,
                    self.ledger.transactions,
                    accounts=self.ledger.accounts,
                    series=self.ledger.series,
                    categories=categories,
                )
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self,
            "Exported",
            f"{len(self.ledger.transactions):,} transactions written to\n{written}",
        )
