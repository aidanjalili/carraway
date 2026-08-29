#!/usr/bin/env bash
#
# Carraway installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aidanjalili/carraway/main/install.sh | bash
#
# Installs into a virtualenv under ~/.local/share/carraway, puts `carraway` on
# the PATH, and adds a desktop entry so it appears in the applications menu
# like anything else.
#
# Nothing here needs root. The only thing written outside the install
# directory is a launcher and a .desktop file, both under ~/.local.

set -euo pipefail

REPO="https://github.com/aidanjalili/carraway.git"
PREFIX="${CARRAWAY_HOME:-$HOME/.local/share/carraway}"
SOURCE="$PREFIX/src"
VENV="$PREFIX/venv"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

# Piping into bash means stdin is the script, so prompts must read from the
# terminal explicitly or they consume the script itself.
if [ -e /dev/tty ]; then exec 3</dev/tty; else exec 3<&0; fi
ask() { printf '%s' "$1"; IFS= read -r -u 3 REPLY || REPLY=""; printf '%s' "$REPLY"; }

bold "Carraway — a local-first money manager"
dim  "Your data stays on this machine. Nothing is uploaded anywhere."
echo

# -- prerequisites --------------------------------------------------------

command -v git >/dev/null || die "git is required. Install it and run this again."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
            PYTHON="$candidate"; break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is required."
dim "Using $($PYTHON --version)"

# The venv module is packaged separately on Debian and Ubuntu.
"$PYTHON" -m venv --help >/dev/null 2>&1 || \
    die "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"

# -- fetch ----------------------------------------------------------------

mkdir -p "$PREFIX"
if [ -d "$SOURCE/.git" ]; then
    dim "Updating existing checkout…"
    git -C "$SOURCE" pull --quiet --ff-only || warn "Could not fast-forward; keeping what is there."
else
    dim "Fetching Carraway…"
    git clone --quiet --depth 1 "$REPO" "$SOURCE"
fi

# -- install --------------------------------------------------------------

dim "Setting up the environment (this takes a minute)…"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip

EXTRAS="gui,sync"
if ! "$VENV/bin/python" -m pip install --quiet -e "$SOURCE[$EXTRAS]"; then
    warn "The desktop interface could not be installed — Qt may not have a build for this system."
    warn "Carrying on with the command line only."
    "$VENV/bin/python" -m pip install --quiet -e "$SOURCE" || die "Installation failed."
    EXTRAS=""
fi

mkdir -p "$BIN"
for tool in carraway carraway-gui; do
    [ -x "$VENV/bin/$tool" ] || continue
    ln -sf "$VENV/bin/$tool" "$BIN/$tool"
done

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "Add $BIN to your PATH to run 'carraway' directly." ;;
esac

# -- desktop entry --------------------------------------------------------

if [ -n "$EXTRAS" ]; then
    mkdir -p "$APPS" "$ICONS"
    cp "$SOURCE/packaging/carraway.svg" "$ICONS/carraway.svg" 2>/dev/null || true
    cat > "$APPS/carraway.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Carraway
GenericName=Money Manager
Comment=Track subscriptions, spending and net worth, entirely on your own machine
Exec=$VENV/bin/carraway-gui
Icon=carraway
Terminal=false
Categories=Office;Finance;
Keywords=budget;money;finance;subscriptions;
StartupWMClass=carraway-gui
DESKTOP
    chmod +x "$APPS/carraway.desktop"
    command -v update-desktop-database >/dev/null && \
        update-desktop-database "$APPS" 2>/dev/null || true
    dim "Added Carraway to your applications menu."
fi

# -- bank sync ------------------------------------------------------------

echo
bold "Bank sync (optional)"
cat <<'EXPLAIN'
Carraway can pull transactions automatically through SimpleFIN Bridge, which
costs about $15/year paid to them directly. You hold the account; Carraway
never sees a bank password.

Without it, everything still works — you import CSV, OFX or QFX statements
downloaded from your bank, which is free and always available.
EXPLAIN
echo
answer=$(ask "Set up SimpleFIN now? You will need a setup token. [y/N] ")
case "$answer" in
    [Yy]*)
        echo
        dim "Find your token at https://beta-bridge.simplefin.org — it is claimed once."
        token=$(ask "Setup token: ")
        if [ -n "$token" ]; then
            "$VENV/bin/carraway" simplefin setup --token "$token" --yes \
                && "$VENV/bin/carraway" sync simplefin --days 0 --link-all \
                || warn "Sync did not complete. Run 'carraway simplefin setup' to try again."
        else
            warn "Nothing entered; skipping."
        fi
        ;;
    *)
        dim "Skipped. Set it up later with: carraway simplefin setup"
        ;;
esac

# -- scheduled sync -------------------------------------------------------

if command -v systemctl >/dev/null && [ -n "$(command -v "$VENV/bin/carraway")" ]; then
    echo
    answer=$(ask "Sync automatically once a week? [y/N] ")
    case "$answer" in
        [Yy]*) "$VENV/bin/carraway" schedule --when weekly || \
                   warn "Could not install the timer." ;;
        *) dim "Skipped. Set it up later with: carraway schedule --when weekly" ;;
    esac
fi

echo
bold "Done."
echo
echo "  carraway-gui                     open the desktop app"
echo "  carraway import statement.csv    load a statement"
echo "  carraway subscriptions           what you pay for on a schedule"
echo "  carraway --help                  everything else"
echo
dim "Installed in $PREFIX. Remove it with: rm -rf $PREFIX $APPS/carraway.desktop"
