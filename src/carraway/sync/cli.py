"""Commands for connecting and syncing external providers.

Kept out of carraway.cli so the main CLI never imports the sync package, and
so someone who only ever imports files pays nothing for code they do not use.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date, timedelta

from ..core import db
from . import credentials

_SIMPLEFIN_URL = "simplefin-access-url"
_VENMO_TOKEN = "venmo-token"
_VENMO_USER = "venmo-user-id"
_VENMO_DEVICE = "venmo-device-id"
_VENMO_ACCOUNT = "venmo-account-id"


def _persist(conn, result, label: str) -> int:
    """Write a SyncResult through the same path a file import takes."""
    for account in result.accounts:
        db.upsert_account(conn, account)
    inserted, skipped = db.insert_transactions(conn, result.transactions)

    print(f"{label}: {inserted} new transaction(s)")
    if skipped:
        print(f"  {skipped} already present, skipped")
    for warning in result.warnings[:10]:
        print(f"  warning: {warning}", file=sys.stderr)
    return inserted


# -- SimpleFIN -----------------------------------------------------------


def cmd_simplefin_setup(args: argparse.Namespace) -> int:
    from .simplefin import SimpleFinError, claim_setup_token

    print("Paste the setup token from your SimpleFIN Bridge account.")
    print("It is claimed once and exchanged for a durable access URL.\n")
    print(f"The result will be stored in: {credentials.describe_store()}\n")
    token = args.token or getpass.getpass("Setup token (hidden): ")
    if not token.strip():
        print("Nothing entered.", file=sys.stderr)
        return 1

    try:
        access_url = claim_setup_token(token)
    except SimpleFinError as exc:
        print(f"Could not claim the token: {exc}", file=sys.stderr)
        return 1

    where = credentials.store(_SIMPLEFIN_URL, access_url)
    print(f"\nConnected. Access URL saved to {where}.")
    print("Run 'carraway sync simplefin' to pull transactions.")
    return 0


def cmd_simplefin_sync(args: argparse.Namespace) -> int:
    from .simplefin import SimpleFinError, SimpleFinProvider

    access_url = credentials.load(_SIMPLEFIN_URL)
    if not access_url:
        print("Not connected yet. Run 'carraway simplefin setup' first.", file=sys.stderr)
        return 1

    conn = db.connect(args.database)
    # Re-use the local ids already assigned to these external accounts, or a
    # second sync would create a parallel set of duplicate accounts.
    known = {a.external_id: a.id for a in db.list_accounts(conn) if a.external_id}
    provider = SimpleFinProvider(access_url, account_ids=known)

    since = date.today() - timedelta(days=args.days) if args.days else None
    try:
        result = provider.fetch(since=since, pending=args.pending)
    except SimpleFinError as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    _persist(conn, result, "SimpleFIN")
    for account in result.accounts:
        print(f"  {account.name} ({account.institution})")
    return 0


def cmd_simplefin_forget(args: argparse.Namespace) -> int:
    removed = credentials.delete(_SIMPLEFIN_URL)
    print("Access URL removed." if removed else "Nothing stored.")
    print("Revoke it at SimpleFIN Bridge too, so it cannot be used again.")
    return 0


# -- Venmo ---------------------------------------------------------------


def cmd_venmo_login(args: argparse.Namespace) -> int:
    from .venmo_api import (
        TwoFactorRequired,
        VenmoError,
        log_in,
        new_device_id,
        send_two_factor_code,
        submit_two_factor_code,
    )

    print("Venmo has no official API, so Carraway signs in the way the mobile")
    print("app does. This is against Venmo's terms of service and they may")
    print("suspend accounts for it. Venmo's CSV export needs none of this.\n")
    print("Your password is used once and never stored. Only the resulting")
    print(f"token is kept, in {credentials.describe_store()}.")
    print("That token is not read-only: anyone holding it can move money, so")
    print("run 'carraway venmo logout' to revoke it when you are done.\n")
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 0

    device_id = credentials.load(_VENMO_DEVICE) or new_device_id()
    username = args.username or input("Venmo email, phone or username: ").strip()
    password = getpass.getpass("Venmo password (hidden, not stored): ")

    try:
        token, user_id = log_in(username, password, device_id)
    except TwoFactorRequired as challenge:
        print("\nVenmo wants a code. Sending one by SMS...")
        try:
            send_two_factor_code(challenge.otp_secret, device_id)
            code = input("Code from SMS: ").strip()
            token, user_id = submit_two_factor_code(code, challenge.otp_secret, device_id)
        except VenmoError as exc:
            print(f"Two-factor failed: {exc}", file=sys.stderr)
            return 1
    except VenmoError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Not security, just hygiene: drop the only reference promptly rather
        # than leaving it live for the rest of the command.
        del password

    credentials.store(_VENMO_DEVICE, device_id)
    credentials.store(_VENMO_USER, user_id)
    where = credentials.store(_VENMO_TOKEN, token)
    print(f"\nSigned in. Token saved to {where}.")
    print("Run 'carraway sync venmo' to pull transactions.")
    return 0


def cmd_venmo_sync(args: argparse.Namespace) -> int:
    from .venmo_api import VenmoError, VenmoProvider

    token = credentials.load(_VENMO_TOKEN)
    user_id = credentials.load(_VENMO_USER)
    if not token or not user_id:
        print("Not signed in. Run 'carraway venmo login' first.", file=sys.stderr)
        return 1

    conn = db.connect(args.database)
    account_id = credentials.load(_VENMO_ACCOUNT)
    if not account_id:
        existing = [a for a in db.list_accounts(conn) if a.external_id == user_id]
        account_id = existing[0].id if existing else f"venmo{user_id[:7]}"
        credentials.store(_VENMO_ACCOUNT, account_id)

    since = date.today() - timedelta(days=args.days) if args.days else None
    try:
        result = VenmoProvider(token, user_id, account_id).fetch(since=since)
    except VenmoError as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    _persist(conn, result, "Venmo")
    return 0


def cmd_venmo_logout(args: argparse.Namespace) -> int:
    from .venmo_api import log_out

    token = credentials.load(_VENMO_TOKEN)
    if not token:
        print("Not signed in.")
        return 0
    # Revoke server-side first: the token never expires on its own, so merely
    # forgetting it locally would leave a working credential in the wild.
    revoked = log_out(token)
    credentials.delete(_VENMO_TOKEN)
    credentials.delete(_VENMO_USER)
    print(
        "Signed out and token revoked at Venmo."
        if revoked
        else "Local token removed, but Venmo did not confirm revocation."
    )
    return 0


def register(sub: argparse._SubParsersAction, database_default: str) -> None:
    """Attach the provider commands to the main parser."""
    sf = sub.add_parser("simplefin", help="connect a bank through SimpleFIN Bridge")
    sf_sub = sf.add_subparsers(dest="simplefin_command", required=True)
    sf_setup = sf_sub.add_parser("setup", help="claim a SimpleFIN setup token")
    sf_setup.add_argument("--token", help="setup token (prompted for if omitted)")
    sf_setup.set_defaults(func=cmd_simplefin_setup)
    sf_forget = sf_sub.add_parser("forget", help="remove the stored access URL")
    sf_forget.set_defaults(func=cmd_simplefin_forget)

    vm = sub.add_parser("venmo", help="connect Venmo (unofficial API; see the README)")
    vm_sub = vm.add_subparsers(dest="venmo_command", required=True)
    vm_login = vm_sub.add_parser("login", help="sign in and store a token")
    vm_login.add_argument("--username", help="email, phone or username")
    vm_login.set_defaults(func=cmd_venmo_login)
    vm_logout = vm_sub.add_parser("logout", help="revoke the token at Venmo and forget it")
    vm_logout.set_defaults(func=cmd_venmo_logout)

    sync = sub.add_parser("sync", help="pull transactions from a connected provider")
    sync_sub = sync.add_subparsers(dest="provider", required=True)
    for name, handler in (("simplefin", cmd_simplefin_sync), ("venmo", cmd_venmo_sync)):
        parser = sync_sub.add_parser(name, help=f"sync from {name}")
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help="how far back to fetch, 0 for everything (default: %(default)s)",
        )
        if name == "simplefin":
            parser.add_argument(
                "--pending", action="store_true", help="include pending transactions"
            )
        parser.set_defaults(func=handler)
