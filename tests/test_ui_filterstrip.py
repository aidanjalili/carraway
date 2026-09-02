"""Dragging filter chips into a different order, and remembering it.

The reordering itself is easy to get subtly wrong in ways that only show up
later: ids drifting out of step with positions makes `tabData` return another
tab's data, and a saved order written by an older version can strand the user
with a strip that is missing a tab. Both are covered here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI tests need the [gui] extra")

from PySide6.QtWidgets import QApplication  # noqa: E402

from carraway.core import db  # noqa: E402
from carraway.core.models import Account, AccountType  # noqa: E402
from carraway.ui.data import Ledger  # noqa: E402
from carraway.ui.views.subscriptions import SubscriptionsView  # noqa: E402
from carraway.ui.widgets import FilterStrip  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    path = tmp_path / "strip.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    conn.close()
    led = Ledger(path)
    led.load()
    return led


def test_moving_a_chip_reorders_the_labels(app):
    strip = FilterStrip()
    for label in ("One", "Two", "Three", "Four"):
        strip.addTab(label)
    strip.moveTab(0, 2)
    assert strip.labels() == ["Two", "Three", "One", "Four"]


def test_moving_a_chip_keeps_its_data_with_it(app):
    """Ids must stay equal to positions or tabData reads the wrong row."""
    strip = FilterStrip()
    for index, label in enumerate(("One", "Two", "Three")):
        strip.addTab(label)
        strip.setTabData(index, f"data-{label}")
    strip.moveTab(2, 0)
    assert strip.labels() == ["Three", "One", "Two"]
    for index, label in enumerate(strip.labels()):
        assert strip.tabData(index) == f"data-{label}"


def test_a_move_to_the_same_place_is_a_no_op(app):
    strip = FilterStrip()
    for label in ("One", "Two"):
        strip.addTab(label)
    strip.moveTab(1, 1)
    strip.moveTab(0, 9)  # out of range
    strip.moveTab(-1, 0)
    assert strip.labels() == ["One", "Two"]


def test_current_index_still_finds_the_checked_chip(app):
    strip = FilterStrip()
    for label in ("One", "Two", "Three"):
        strip.addTab(label)
    strip.setCurrentIndex(2)
    strip.moveTab(2, 0)
    assert strip.tabText(strip.currentIndex()) == "Three"


# -- the saved order ----------------------------------------------------


def test_the_default_puts_the_catch_alls_last(app, ledger):
    view = SubscriptionsView(ledger)
    assert view.tabs.labels()[-2:] == ["Hidden", "All"]


def test_a_dragged_order_is_saved_and_restored(app, ledger):
    view = SubscriptionsView(ledger)
    view.tabs.moveTab(0, 3)
    wanted = view.tabs.labels()
    view.tabs.orderChanged.emit(wanted)

    ledger.load()
    assert ledger.setting("subscriptions_tab_order") == wanted
    assert SubscriptionsView(ledger).tabs.labels() == wanted


def test_a_saved_order_from_an_older_version_still_shows_every_tab(app, ledger):
    """A tab added later must not go missing because it is not in the list."""
    ledger.save_setting("subscriptions_tab_order", ["All", "Bills"])
    labels = SubscriptionsView(ledger).tabs.labels()
    assert labels[:2] == ["All", "Bills"]
    assert set(labels) == set(SubscriptionsView.DEFAULT_TABS)


def test_a_saved_order_naming_a_tab_that_no_longer_exists_is_ignored(app, ledger):
    ledger.save_setting("subscriptions_tab_order", ["Ghosts", "Bills"])
    labels = SubscriptionsView(ledger).tabs.labels()
    assert "Ghosts" not in labels
    assert labels[0] == "Bills"
    assert set(labels) == set(SubscriptionsView.DEFAULT_TABS)


def test_rubbish_in_the_setting_falls_back_to_the_default(app, ledger):
    ledger.save_setting("subscriptions_tab_order", "not a list")
    assert SubscriptionsView(ledger).tabs.labels() == list(SubscriptionsView.DEFAULT_TABS)
