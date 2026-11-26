#!/bin/sh
# uninstall.sh - remove the CAO window installed by install.sh.
# Quits the window when it is running, removes its machine entry, stops its core session, removes the
# launcher and moves its home to <home>.removed-<epoch>. It never touches your main Herdr home, CAO's
# own core session or any CAO terminal.
# Exit codes: 1 an hc step failed (the home is kept so a rerun can retry), 2 bad usage or a refusal.
set -eu
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/bin/canon.sh"
. "$HERE/bin/app-copy.sh"
unset HC_TERMINAL_ID HC_TERMINAL_SOCKET HC_SCOPE HC_SOCKET HC_SESSION
die() { code=$1; shift; echo "uninstall: $*" >&2; exit "$code"; }

XDG=${XDG_CONFIG_HOME:-$HOME/.config}
HOME_DIR=$XDG/herdr-app-cao MAIN_HOME=$XDG/herdr-app
while [ $# -gt 0 ]; do
    case $1 in
        --home) [ $# -ge 2 ] && [ -n "$2" ] || die 2 "--home needs a folder"; HOME_DIR=$2; shift 2 ;;
        --main-home) [ $# -ge 2 ] && [ -n "$2" ] || die 2 "--main-home needs a folder"; MAIN_HOME=$2; shift 2 ;;
        -h|--help) echo "usage: uninstall.sh [--home DIR] [--main-home DIR]"; exit 0 ;;
        *) die 2 "unknown option $1" ;;
    esac
done
command -v jq >/dev/null 2>&1 || die 2 "jq is required"
HOME_DIR=$(canon "$HOME_DIR"); MAIN_HOME=$(canon "$MAIN_HOME")
HOME_NOW=$(canon "$HOME")
if [ -n "${HERDR_APP_HOME:-}" ]; then
    APP_HOME_NOW=$(canon "$HERDR_APP_HOME")
    if [ "$HOME_DIR" = "$APP_HOME_NOW" ]; then
        die 2 "refusing $HOME_DIR: it is the home this shell's Herdr app uses (\$HERDR_APP_HOME), so this shell runs inside the CAO window; run uninstall from another terminal, not from inside the CAO window"
    fi
    if contains "$HOME_DIR" "$APP_HOME_NOW" || contains "$APP_HOME_NOW" "$HOME_DIR"; then
        die 2 "refusing $HOME_DIR: it holds or lies inside the home this shell's Herdr app uses (\$HERDR_APP_HOME)"
    fi
fi
if contains "$HOME_DIR" "$MAIN_HOME" || contains "$MAIN_HOME" "$HOME_DIR" || contains "$HOME_DIR" "$HOME_NOW"; then
    die 2 "refusing $HOME_DIR: it is, holds or lies inside your main Herdr home or your home folder"
fi
MANIFEST=$HOME_DIR/cao-window-install.json
[ -f "$MANIFEST" ] || die 2 "nothing to remove: $MANIFEST does not exist"
jq -e 'type == "object"' "$MANIFEST" >/dev/null 2>&1 || die 2 "$MANIFEST is not a JSON object; nothing was changed"

# What the manifest names is read again here, as install.sh would accept it: the file can be edited by hand or by
# another program, and these values become an hc session name, an hc argument and a path to delete. A value that is
# not text reads as empty.
str() { jq -r --arg k "$1" 'if (.[$k] | type) == "string" then .[$k] else "" end' "$MANIFEST"; }
CORE=$(str core); CAO_CORE=$(str caoCore); MACHINE=$(str machine)
LAUNCHER_NAME=$(str launcher); BIN_DIR=$(str binDir)
valid_name() { case $2 in ''|.|..|-*|*[!A-Za-z0-9._-]*) die 2 "the manifest's $1 value is not a name install.sh would accept (letters, digits, dot, dash or underscore, no leading dash); nothing was changed" ;; esac; }
[ "$CORE" != "$CAO_CORE" ] || die 2 "the manifest names core session '$CORE', which is CAO's own core; refusing to stop it"
case $CORE in cao|default) die 2 "the manifest names core session '$CORE'; refusing to stop it" ;; esac
valid_name core "$CORE"
valid_name machine "$MACHINE"
# The launcher is a file name in the bin folder, as install.sh makes it (no slash, not . or ..), and the bin dir is
# absolute: a relative one would be resolved against whatever directory uninstall runs in.
case $LAUNCHER_NAME in ''|.|..|*/*) die 2 "the manifest's launcher value is not a file name; nothing was changed" ;; esac
case $BIN_DIR in /*) ;; *) die 2 "the manifest does not give an absolute path for the launcher; nothing was changed" ;; esac
LAUNCHER=$BIN_DIR/$LAUNCHER_NAME
# The app copy (CAO.app) is removed only when it is what install.sh made: an absolute path to a folder named *.app (not a
# link) whose Info.plist carries our key or that holds our marker file. The path comes from the manifest, so it is checked here, before anything is
# changed, and again just before the removal. A path that fails is never touched and never globbed.
APP_COPY=$(jq -r 'if (.appCopy | type) == "object" and (.appCopy.path | type) == "string" then .appCopy.path else "" end' "$MANIFEST")
if [ -n "$APP_COPY" ]; then
    case $APP_COPY in
        /*.app) [ "${APP_COPY##*/}" != .app ] || APP_COPY_BAD=1 ;;
        *) APP_COPY_BAD=1 ;;
    esac
    [ -z "${APP_COPY_BAD:-}" ] || die 2 "the manifest's appCopy path is not an absolute path to a folder named *.app: $APP_COPY; nothing was changed"
    if [ -e "$APP_COPY" ] || [ -L "$APP_COPY" ]; then
        app_copy_is_ours "$APP_COPY" \
            || die 2 "the manifest names $APP_COPY as the app copy, but it is not a folder whose Info.plist has $COPY_MARK_KEY or that holds $COPY_MARKER_FILE (not a copy install.sh made); nothing was changed"
    fi
fi

# Quit the window first, then the machine entry, then the core (docs/herdr-app.md: stopping the core
# first lets the app's reconnect start it again).
if HERDR_APP_HOME=$HOME_DIR herdr-app status >/dev/null 2>&1; then
    HERDR_APP_HOME=$HOME_DIR herdr-app 'do' app.quit || echo "uninstall: warning: could not quit the window" >&2
fi
# A failing hc call stops the run with the home and its manifest in place, so a rerun can finish the job.
# The machine entry is removed only when the core still lists it (the check install.sh uses), so a rerun
# after a partial uninstall does not ask hc to remove what is already gone, and no other machine is touched.
have_machine() { HC_SESSION=$CORE hc machine ls --json 2>/dev/null | jq -e --arg m "$MACHINE" '[.. | objects | select(.name? == $m)] | length > 0' >/dev/null 2>&1; }
if have_machine; then
    HC_SESSION=$CORE hc machine rm "$MACHINE" \
        || die 1 "hc machine rm $MACHINE failed in core session '$CORE'; the window's home was left in place, fix hc and run uninstall again"
fi
HC_SESSION=$CORE hc server stop \
    || die 1 "hc server stop failed for core session '$CORE'; the window's home was left in place, fix hc and run uninstall again"
# The launcher is a convenience, so a file that is not the one install.sh wrote is left where it is, with a note, and the
# uninstall goes on. install.sh's launcher carries this header line (bin/herdr-cao.in) as a whole line; a folder, a
# symlink and a file without the line are never deleted. An edited launcher that still carries the line is removed.
LAUNCHER_MARK='# Written by examples/herdr-app-cao-window/install.sh. Re-run the installer to change it.'
if [ -L "$LAUNCHER" ]; then
    echo "uninstall: note: left $LAUNCHER in place: it is a symbolic link, not the launcher install.sh wrote" >&2
elif [ -f "$LAUNCHER" ]; then
    if grep -qxF -- "$LAUNCHER_MARK" "$LAUNCHER" 2>/dev/null; then
        rm -f -- "$LAUNCHER"
    else
        echo "uninstall: note: left $LAUNCHER in place: it does not carry the header line install.sh writes, so it is not the launcher it wrote" >&2
    fi
elif [ -e "$LAUNCHER" ]; then
    echo "uninstall: note: left $LAUNCHER in place: it is not a file" >&2
else
    echo "uninstall: warning: the launcher $LAUNCHER named in the manifest was not found; nothing to remove there" >&2
fi
# An earlier install.sh left a backup of a changed launcher next to it, as <launcher>.bak-<epoch>, where PATH finds it; it now
# keeps those in the window's home. A backup that is a regular file with the header line is ours and goes; the user's own file
# of that name stays.
for backup in "$BIN_DIR"/*; do
    case $backup in "$LAUNCHER".bak-*) ;; *) continue ;; esac
    [ -f "$backup" ] && [ ! -L "$backup" ] || continue
    grep -qxF -- "$LAUNCHER_MARK" "$backup" 2>/dev/null || continue
    rm -f -- "$backup" && echo "uninstall: removed $backup"
done
# The app copy goes before the home does, so a failure leaves the manifest that names it and a rerun can finish. It is
# removed, not moved aside like the home: it holds nothing of the user's, and a rerun of install.sh builds it again.
if [ -n "$APP_COPY" ]; then
    if [ -e "$APP_COPY" ] || [ -L "$APP_COPY" ]; then
        app_copy_is_ours "$APP_COPY" || die 1 "$APP_COPY is no longer a copy install.sh made; it was left in place, and so was the window's home"
        rm -r -- "$APP_COPY" || die 1 "cannot remove $APP_COPY; the window's home was left in place, fix that and run uninstall again"
        echo "uninstall: removed $APP_COPY"
    else
        echo "uninstall: warning: the app copy $APP_COPY named in the manifest was not found; nothing to remove there" >&2
    fi
fi
# A name that is taken (a second uninstall in the same second) would make mv move the home inside it.
stamp=$(date +%s)
DEST=$HOME_DIR.removed-$stamp
while [ -e "$DEST" ] || [ -L "$DEST" ]; do stamp=$((stamp + 1)); DEST=$HOME_DIR.removed-$stamp; done
mv -- "$HOME_DIR" "$DEST"
echo "uninstall: done. The window's home is kept as $DEST; CAO and its terminals were not touched."
