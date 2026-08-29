"""Entry point for the desktop app: `carraway-gui`, or `python -m carraway.ui`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..core import db
from .main_window import MainWindow
from .theme import activate, stylesheet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carraway-gui", description="Carraway desktop app.")
    parser.add_argument("--version", action="version", version=f"carraway {__version__}")
    parser.add_argument(
        "--database",
        default=str(db.default_db_path()),
        help="path to the Carraway database (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Carraway")
    app.setOrganizationName("Carraway")

    # Follow whichever theme the desktop is already in, rather than imposing
    # one: this is an app people leave open all day.
    window_colour = app.palette().color(QPalette.ColorRole.Window)
    app.setStyleSheet(stylesheet(activate(window_colour.lightness() < 128)))

    window = MainWindow(Path(args.database))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
