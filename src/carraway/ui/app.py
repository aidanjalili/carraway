"""Entry point for the desktop app: `carraway-gui`, or `python -m carraway.ui`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..core import db
from .assets import app_icon
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
    app.setApplicationDisplayName("Carraway")
    # Wayland identifies a window by its desktop file rather than by a class
    # name, and without this the compositor cannot match the running window to
    # the installed launcher — so the taskbar shows a placeholder even when the
    # icon is installed correctly.
    app.setDesktopFileName("carraway")
    app.setWindowIcon(app_icon())

    # Follow whichever theme the desktop is already in, rather than imposing
    # one: this is an app people leave open all day.
    window_colour = app.palette().color(QPalette.ColorRole.Window)
    app.setStyleSheet(stylesheet(activate(window_colour.lightness() < 128)))

    window = MainWindow(Path(args.database))
    window.show()
    code = app.exec()

    # A request can outlast every wait on the way out: a sync is several calls
    # with a minute's timeout each. Returning from here destroys the window
    # and the application, and with them any thread still running, which Qt
    # answers with an abort and a core dump. So when one is still going, the
    # process ends without that teardown. Nothing is lost by it: every write
    # the thread makes is its own SQLite transaction, and a phone entry is
    # only claimed after it has been stored, so an interrupted collection is
    # simply collected again.
    from . import sync_worker
    from .views import pocket

    if sync_worker.any_running() or pocket.any_in_flight():
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
