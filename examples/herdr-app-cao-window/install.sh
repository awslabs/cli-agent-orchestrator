#!/bin/sh
# install.sh - install the CAO window for the Herdr app (examples/herdr-app-cao-window).
# Run it on the machine where the Herdr app runs. Needs jq and hc; fetches nothing.
# Exit codes: 1 a file or folder could not be written, 2 bad usage or refusal, 3 version too old,
# 4 platform unsupported, 5 base layout drifted.
# Every refusal happens before the first file is written: everything is staged in a temp folder and
# written in the last phase. On macOS the phase starts with the app copy (CAO.app), built next to its
# target and moved into place only when it is signed.
set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/bin/launcher-fill.sh"
. "$HERE/bin/canon.sh"
. "$HERE/bin/app-copy.sh"

MIN_HC=0.1.21
MIN_APP=0.1.16                       # the version the example was built and checked against
MODS="cao-window-side cao-window-panel cao-window-tabs cao-window-status cao-window-brand"
# Formats read from a real install: the flat string key "frame.welcomed" in shared.json and
# the boolean `telemetry` in settings.json. Each is one constant, so a Herdr that changes them is a one-line fix.
SHARED_EDIT='.layout = "cao-window" | ."frame.welcomed" = "finished"'
TELEMETRY_KEY='.telemetry'

# The two switches record the outcome of a check on a real Herdr, not a user's choice, so they come from the environment.
# Only the exact values below change anything; the summary line says what is in effect.
TOAST=0
case ${TOAST_MOD:-} in
    1) TOAST=1 ;;
    ''|0) ;;
    *) echo "install: warning: TOAST_MOD=$TOAST_MOD is not 1, so the toast mod stays off" >&2 ;;
esac
IMPORTS=copy
case ${MOD_IMPORTS:-} in
    bundle) IMPORTS=bundle ;;
    ''|copy) ;;
    *) echo "install: warning: MOD_IMPORTS=$MOD_IMPORTS is not 'bundle', so shared modules are copied" >&2 ;;
esac
[ "$TOAST" = 1 ] && MODS="$MODS cao-window-toast"

unset HC_TERMINAL_ID HC_TERMINAL_SOCKET HC_SCOPE HC_SOCKET HC_SESSION

# bundle_mod LIBDIR MODDIR: bundle mode (all of a mod's code in one file). The shared modules first, then the mod folder's other .js
# files, then main.js. Relative imports (also multi-line ones, also `import "x"`) are dropped, the
# "export " keyword of a declaration is removed and `export { ... };` lists are removed. Imports of the
# host (herdr, gpui-kit) stay, and an ES import may sit anywhere at the top level. A line that starts
# with `import ` inside a template string would be dropped too, so the templates use `from x import y`.
bundle_one() {
    awk -v q="'" '
        function complete(s,   re) {
            re = "(from|import)[ ]*[\"" q "][^\"" q "]*[\"" q "][ ]*;?[ ]*$"
            return s ~ re
        }
        function relative(s,   re) {
            re = "[\"" q "]\\.\\.?/[^\"" q "]*[\"" q "][ ]*;?[ ]*$"
            return s ~ re
        }
        BEGIN { re_import = "^import[ {\"" q "]" }
        $0 ~ re_import {
            stmt = $0
            while (!complete(stmt) && (getline more) > 0) stmt = stmt "\n" more
            if (!relative(stmt)) print stmt
            next
        }
        /^export[ ]*\{[^}]*\}[ ]*;?[ ]*$/ { next }
        { line = $0; if (line ~ /^export (async function|function|const|let|class) /) sub(/^export /, "", line); print line }
    ' "$1"
}
bundle_mod() {
    for f in "$1"/*.js; do [ -e "$f" ] && bundle_one "$f"; done
    for f in "$2"/*.js; do [ -e "$f" ] && { [ "$(basename "$f")" = main.js ] || bundle_one "$f"; }; done
    [ ! -e "$2/main.js" ] || bundle_one "$2/main.js"
    return 0
}

die() { code=$1; shift; echo "install: $*" >&2; exit "$code"; }
usage() {
    cat <<'EOF'
usage: install.sh (--cao-ssh-host HOST | --local) [options]
  --cao-ssh-host HOST   CAO runs on HOST (hc machine add HOST --session <cao core>)
  --local               CAO runs on this machine (hc machine add --via "sh -c")
  --home DIR            app home of the CAO window      (default: $XDG_CONFIG_HOME/herdr-app-cao)
  --core-session NAME   core session of the window      (default: caowin; never cao or default)
  --machine NAME        name of the CAO machine entry   (default: cao)
  --cao-core-session N  CAO's own core session          (default: cao)
  --cao-port PORT       cao-server port                 (default: 9889)
  --bin-dir DIR         where the launcher goes         (default: $HOME/.local/bin)
  --launcher-name NAME  launcher file name              (default: herdr-cao)
  --app PATH            the Herdr app                   (default: /Applications/Herdr.app on macOS)
  --app-copy-dir DIR    the folder that holds CAO.app   (default: $HOME/Applications; macOS only)
  --no-app-copy         do not make CAO.app: the launcher opens the Herdr app itself
  --main-home DIR       your normal Herdr home, read only (default: $XDG_CONFIG_HOME/herdr-app)
  --telemetry on|off    set telemetry in the window's settings.json
  --prop KEY=JSON       stamp one more layout prop on every CAO block (repeatable)
  --force               replace a layout you customized (kept as .bak-<epoch>)
names (--core-session, --machine, --cao-core-session) use letters, digits, dot, dash and underscore.
environment (documented in the README):
  TOAST_MOD=1           install the toast mod and set the layout's root toast key (default 0: off)
  MOD_IMPORTS=bundle    one main.js per mod instead of a flat copy of lib/*.js (default copy)
EOF
}

XDG=${XDG_CONFIG_HOME:-$HOME/.config}
HOME_DIR=$XDG/herdr-app-cao CORE=caowin MACHINE=cao SSH_HOST='' LOCAL=0 CAO_CORE=cao CAO_PORT=9889
BIN_DIR=$HOME/.local/bin LAUNCHER_NAME=herdr-cao APP='' MAIN_HOME=$XDG/herdr-app TELEMETRY='' PROP_ARGS='' FORCE=0
APP_COPY_DIR='' NO_APP_COPY=0
while [ $# -gt 0 ]; do
    opt=$1
    case $opt in
        --local) LOCAL=1; shift; continue ;;
        --force) FORCE=1; shift; continue ;;
        --no-app-copy) NO_APP_COPY=1; shift; continue ;;
        -h|--help) usage; exit 0 ;;
        --cao-ssh-host|--home|--core-session|--machine|--cao-core-session|--cao-port|--bin-dir|--launcher-name|--app|--app-copy-dir|--main-home|--telemetry|--prop)
            [ $# -ge 2 ] || die 2 "$opt needs a value" ;;
        *) usage >&2; die 2 "unknown option $opt" ;;
    esac
    val=$2; shift 2
    case $opt in
        --home|--main-home|--bin-dir|--app|--app-copy-dir) [ -n "$val" ] || die 2 "$opt must not be empty (an empty path would mean the working directory)" ;;
    esac
    case $opt in
        --cao-ssh-host) SSH_HOST=$val ;;       --home) HOME_DIR=$val ;;
        --core-session) CORE=$val ;;           --machine) MACHINE=$val ;;
        --cao-core-session) CAO_CORE=$val ;;   --cao-port) CAO_PORT=$val ;;
        --bin-dir) BIN_DIR=$val ;;             --launcher-name) LAUNCHER_NAME=$val ;;
        --app) APP=$val ;;                     --main-home) MAIN_HOME=$val ;;
        --app-copy-dir) APP_COPY_DIR=$val ;;
        --telemetry) TELEMETRY=$val ;;
        --prop) PROP_ARGS="$PROP_ARGS
$val" ;;
    esac
done

# --- usage checks and refusals: nothing has been written yet ---
if [ "$LOCAL" = 1 ] && [ -n "$SSH_HOST" ]; then die 2 "pass --local or --cao-ssh-host, not both"; fi
if [ "$LOCAL" = 0 ] && [ -z "$SSH_HOST" ]; then usage >&2; die 2 "say where CAO runs: --cao-ssh-host HOST or --local"; fi
# A host that starts with "-" would be read by hc as an option; whitespace is never part of a host.
case $SSH_HOST in -*|*[[:space:]]*) die 2 "--cao-ssh-host must be a host name, not an option or text with spaces" ;; esac
case $CAO_PORT in ''|*[!0-9]*) die 2 "--cao-port must be a number from 1 to 65535" ;; esac
CAO_PORT=${CAO_PORT#"${CAO_PORT%%[!0]*}"}     # no leading zeros: the port is stamped on the layout as a JSON number
if [ -z "$CAO_PORT" ] || [ "${#CAO_PORT}" -gt 5 ] || [ "$CAO_PORT" -gt 65535 ]; then die 2 "--cao-port must be a number from 1 to 65535"; fi
case $TELEMETRY in ''|on|off) ;; *) die 2 "--telemetry takes on or off" ;; esac
case $CORE in
    cao|default) die 2 "refusing core session '$CORE': 'cao' is CAO's own core and 'default' is your normal Herdr core" ;;
esac
valid_name() { case $2 in ''|.|..|-*|*[!A-Za-z0-9._-]*) die 2 "$1 must be letters, digits, dot, dash or underscore and must not start with a dash" ;; esac; }
valid_name "--core-session" "$CORE"
valid_name "--machine" "$MACHINE"
valid_name "--cao-core-session" "$CAO_CORE"
if [ "$CORE" = "$CAO_CORE" ]; then die 2 "refusing core session '$CORE': it is CAO's own core session (--cao-core-session); the window needs a core of its own"; fi
case $LAUNCHER_NAME in ''|.|..|*/*) die 2 "--launcher-name must be a file name" ;; esac
command -v jq >/dev/null 2>&1 || die 2 "jq is required (see README, Prerequisites)"
command -v hc >/dev/null 2>&1 || die 3 "hc is not on PATH (herdr-core $MIN_HC or later is required)"

HOME_DIR=$(canon "$HOME_DIR"); MAIN_HOME=$(canon "$MAIN_HOME"); BIN_DIR=$(canon "$BIN_DIR")
# The window's home is written, replaced and (by uninstall) renamed as a whole, so it must not be, hold or
# (for the two app homes) lie inside a folder other things live in: the main app home, the home of the app
# this shell runs under, $HOME, or the launcher folder.
refuse_home() { die 2 "refusing --home $HOME_DIR: $1"; }
if contains "$HOME_DIR" "$MAIN_HOME" || contains "$MAIN_HOME" "$HOME_DIR"; then
    refuse_home "it is, holds or lies inside your main Herdr home ($MAIN_HOME)"
fi
if [ -n "${HERDR_APP_HOME:-}" ]; then
    APP_HOME_NOW=$(canon "$HERDR_APP_HOME")
    if contains "$HOME_DIR" "$APP_HOME_NOW" || contains "$APP_HOME_NOW" "$HOME_DIR"; then
        refuse_home "it is, holds or lies inside the home this shell's Herdr app uses (\$HERDR_APP_HOME, $APP_HOME_NOW)"
    fi
fi
if contains "$HOME_DIR" "$(canon "$HOME")"; then refuse_home "it is or holds your home folder"; fi
if contains "$HOME_DIR" "$BIN_DIR"; then refuse_home "it is or holds the launcher folder ($BIN_DIR)"; fi
# The install sets the home to mode 700 and uninstall renames it, so a folder that already holds other things (a typo for
# ~/Documents) is refused. An empty or missing folder is fine, and so is one this installer made: it holds the manifest, or
# the layout a first run wrote before it was stopped (the manifest is written last).
if [ -d "$HOME_DIR" ] && [ -n "$(ls -A -- "$HOME_DIR" 2>/dev/null)" ] \
    && [ ! -f "$HOME_DIR/cao-window-install.json" ] && [ ! -f "$HOME_DIR/layouts/cao-window.json" ]; then
    refuse_home "it is not empty and holds no cao-window-install.json, so it is not a home this installer made; use an empty or new folder. Nothing was written"
fi
# What the last run asked for, read before this run writes its own manifest: the machine step below uses it.
PREV_EXISTS=false PREV_LOCAL=false PREV_HOST=''
if [ -f "$HOME_DIR/cao-window-install.json" ] && jq -e 'type == "object"' "$HOME_DIR/cao-window-install.json" >/dev/null 2>&1; then
    PREV_EXISTS=true
    PREV_LOCAL=$(jq -r 'if .local == true then "true" else "false" end' "$HOME_DIR/cao-window-install.json")
    PREV_HOST=$(jq -r 'if (.caoSshHost | type) == "string" then .caoSshHost else "" end' "$HOME_DIR/cao-window-install.json")
fi

OS=$(uname -s)
case $OS in
    Darwin) [ -n "$APP" ] || APP=/Applications/Herdr.app ;;
    Linux)
        [ "$LINUX_SUPPORTED" = 1 ] || die 4 "the Herdr app on Linux is unsupported (see README, topology: Linux)"
        [ -n "$APP" ] || die 2 "pass --app PATH: the Herdr app binary on Linux"
        ;;
    *) die 4 "no Herdr app is supported on '$OS' (see README, topologies)" ;;
esac
# The launcher and the manifest outlive this shell's working directory, so they carry absolute paths.
case $APP in /*) ;; *) APP=$PWD/$APP ;; esac

# --- versions ---
ver_ge() {   # ver_ge FOUND REQUIRED: true when FOUND >= REQUIRED, comparing dotted numbers
    awk -v a="$1" -v b="$2" 'BEGIN { split(a, x, "."); split(b, y, "."); for (i = 1; i <= 3; i++) {
        if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 } exit 0 }'
}
semver() { awk 'match($0, /[0-9]+\.[0-9]+\.[0-9]+/) { print substr($0, RSTART, RLENGTH); exit }'; }
HC_FOUND=$(hc --version 2>/dev/null | semver)
{ [ -n "$HC_FOUND" ] && ver_ge "$HC_FOUND" "$MIN_HC"; } || die 3 "hc ${HC_FOUND:-'(version unreadable)'} is too old: herdr-core $MIN_HC or later is required"
if [ "$OS" = Darwin ]; then
    APP_RAW=$(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)
    APP_FOUND=$(printf '%s\n' "$APP_RAW" | semver)
else
    APP_RAW=$MIN_APP; APP_FOUND=$MIN_APP   # Linux: no version command is known yet; reached only when LINUX_SUPPORTED=1
fi
{ [ -n "$APP_FOUND" ] && ver_ge "$APP_FOUND" "$MIN_APP"; } || die 3 "Herdr app ${APP_FOUND:-'(version unreadable)'} at $APP is too old: $MIN_APP or later is required"

# --- the app copy (macOS): every refusal is made here, before anything is written ---
# CAO.app is a clone of the Herdr app with its own name, so the app switcher, the Dock and Mission Control say CAO. It is
# ours while its Info.plist carries $COPY_MARK_KEY, the Herdr version it was made from, or the bundle holds $COPY_MARKER_FILE
# (app-copy.sh); a rerun rebuilds it when that version differs from the app's or the plist lost the key, or when the copy
# is gone, and otherwise leaves it alone.
COPY=''; COPY_BUILD=0; ICON_SRC=$HERE/assets/CAO.icns
if [ "$OS" = Darwin ] && [ "$NO_APP_COPY" = 0 ]; then
    [ -n "$APP_COPY_DIR" ] || APP_COPY_DIR=$HOME/Applications
    APP_COPY_DIR=$(canon "$APP_COPY_DIR")
    COPY=$APP_COPY_DIR/CAO.app
    [ -f "$APP/Contents/Info.plist" ] || die 2 "$APP is not a Herdr app bundle (it has no Contents/Info.plist), so no CAO.app can be made from it; pass --app PATH, or --no-app-copy to open the Herdr app itself"
    [ -f "$ICON_SRC" ] || die 2 "$ICON_SRC is missing from this example; nothing was written"
    for tool in codesign xattr; do
        command -v "$tool" >/dev/null 2>&1 || die 3 "$tool is not on PATH, and CAO.app needs it (pass --no-app-copy to skip the copy)"
    done
    plist_has_tool || die 3 "PlistBuddy was not found (it is /usr/libexec/PlistBuddy on macOS), and CAO.app needs it (pass --no-app-copy to skip the copy)"
    APP_CANON=$(canon "$APP")
    if contains "$APP_CANON" "$COPY" || contains "$COPY" "$APP_CANON"; then
        die 2 "refusing --app-copy-dir $APP_COPY_DIR: CAO.app would be, hold or lie inside the Herdr app ($APP_CANON); nothing was written"
    fi
    if [ -e "$COPY" ] || [ -L "$COPY" ]; then
        app_copy_is_ours "$COPY" \
            || die 2 "refusing to replace $COPY: it is not a copy this installer made (its Info.plist has no $COPY_MARK_KEY and it holds no $COPY_MARKER_FILE). Move it away or pass --app-copy-dir; nothing was written"
        # A copy whose plist lost the key (an update changed it in place) records no version, so it is rebuilt.
        recorded=$(app_copy_mark "$COPY" 2>/dev/null || true)
        [ "$recorded" = "$APP_RAW" ] || COPY_BUILD=1
    else
        COPY_BUILD=1
    fi
    if [ "$COPY_BUILD" = 1 ]; then
        ac_d=$APP_COPY_DIR
        while [ ! -d "$ac_d" ]; do
            if [ -e "$ac_d" ] || [ -L "$ac_d" ]; then die 1 "$ac_d exists and is not a folder; nothing was written"; fi
            ac_d=$(dirname -- "$ac_d")
        done
        { [ -w "$ac_d" ] && [ -x "$ac_d" ]; } || die 1 "cannot write in $ac_d; nothing was written"
    fi
fi

# --- stage everything in a temp folder; write only at the end ---
WORK=$(mktemp -d); STAGE=$WORK/stage
OWN_TMP=
# The folder a copy is built in. Only a folder of exactly this name, which this run made, is ever removed.
APP_NEW=
rm_app_new() {
    case $APP_NEW in /*/.CAO-new.*.app) [ ! -e "$APP_NEW" ] || rm -r -- "$APP_NEW" ;; esac
    APP_NEW=
}
trap 'rm -rf "$WORK"; [ -z "$OWN_TMP" ] || rm -f -- "$OWN_TMP"; rm_app_new' EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "$STAGE/layouts"

PROPS=$(jq -c --arg m "$MACHINE" --argjson p "$CAO_PORT" '. + {machine: $m, caoPort: $p}' "$HERE/layouts/props.default.json")
OLDIFS=$IFS; IFS='
'
set -f   # a JSON value may contain * or [ ]; do not glob it
for kv in $PROP_ARGS; do
    [ -n "$kv" ] || continue
    case $kv in *=*) ;; *) die 2 "--prop expects KEY=JSON, got '$kv'" ;; esac
    [ -n "${kv%%=*}" ] || die 2 "--prop needs a key before the '=': '$kv'"
    # machine and caoPort are set from --machine and --cao-port, which are checked and used for the hc entry and the port
    # check; a --prop that says something else would stamp a value no other part of the install knows about.
    case ${kv%%=*} in
        machine) own=$(jq -nc --arg v "$MACHINE" '$v'); flag=--machine; shown=$MACHINE ;;
        caoPort) own=$CAO_PORT; flag=--cao-port; shown=$CAO_PORT ;;
        *) own='' ;;
    esac
    if [ -n "$own" ] && [ "$(printf '%s' "${kv#*=}" | jq -c . 2>/dev/null)" != "$own" ]; then
        die 2 "--prop ${kv%%=*}=${kv#*=} conflicts with $flag $shown; set it with $flag and leave --prop out"
    fi
    PROPS=$(printf '%s' "$PROPS" | jq -c --arg k "${kv%%=*}" --argjson v "${kv#*=}" '.[$k] = $v') \
        || die 2 "--prop ${kv%%=*}: the value is not JSON"
done
set +f; IFS=$OLDIFS

# Base layout, first found wins: the main home's, then the copy shipped in this example. The app bundle
# holds no layout file, so there is no bundle lookup.
if [ -f "$MAIN_HOME/layouts/main.json" ]; then
    BASE=$MAIN_HOME/layouts/main.json
else
    BASE=$HERE/layouts/main.json
    echo "install: warning: $MAIN_HOME/layouts/main.json not found; using the copy in this example, which may lag the Herdr you run (layouts/main.json.source.md)" >&2
fi
rc=0
jq --argjson props "$PROPS" --argjson toast "$([ "$TOAST" = 1 ] && echo true || echo false)" -f "$HERE/make-window.jq" "$BASE" > "$STAGE/layouts/cao-window.json" 2> "$WORK/jq.err" || rc=$?
if [ "$rc" -ne 0 ]; then
    cat "$WORK/jq.err" >&2
    [ "$rc" -eq 5 ] && die 5 "$BASE does not look like Herdr's main layout; nothing was written"
    die 2 "jq failed on $BASE"
fi
[ -s "$STAGE/layouts/cao-window.json" ] || die 5 "$BASE is empty; nothing was written"

for m in $MODS; do
    mkdir -p "$STAGE/mods/$m"
    # A mod folder holds mod.json, the mod's .js files and its own .svg drawings (the side mod's pin, drawn by the path
    # mods/<mod>/<file>.svg). Anything else in it (a sync tool's temp file, a note, an editor's backup) is not part of the
    # mod and is not installed or listed in the manifest. Herdr's own icons are not copied: every Herdr home has them.
    for f in "$HERE/mods/$m"/*.js "$HERE/mods/$m"/*.svg "$HERE/mods/$m/mod.json"; do
        [ ! -f "$f" ] || cp -- "$f" "$STAGE/mods/$m/"
    done
    if [ "$IMPORTS" = bundle ]; then
        bundle_mod "$HERE/lib" "$HERE/mods/$m" > "$WORK/bundle.new"
        for f in "$STAGE/mods/$m"/*.js; do [ ! -e "$f" ] || rm -- "$f"; done
        mv "$WORK/bundle.new" "$STAGE/mods/$m/main.js"
    else
        cp -- "$HERE"/lib/*.js "$STAGE/mods/$m/"
    fi
done

if [ -f "$HOME_DIR/shared.json" ]; then
    jq -e 'type == "object"' "$HOME_DIR/shared.json" >/dev/null 2>&1 || die 2 "$HOME_DIR/shared.json is not a JSON object; fix or move it"
    jq "$SHARED_EDIT" "$HOME_DIR/shared.json" > "$STAGE/shared.json" || die 2 "$HOME_DIR/shared.json could not be updated; fix or move it"
    # Herdr rewrites shared.json itself; when only its formatting differs from what we would write, keep it as it is.
    if jq -e --slurpfile cur "$HOME_DIR/shared.json" '. == $cur[0]' "$STAGE/shared.json" >/dev/null 2>&1; then
        cp "$HOME_DIR/shared.json" "$STAGE/shared.json"
    fi
else
    jq -n "{} | $SHARED_EDIT" > "$STAGE/shared.json"
fi

SETTINGS_NOTE=
if [ -f "$HOME_DIR/settings.json" ]; then settings_base=$HOME_DIR/settings.json
elif [ -f "$MAIN_HOME/settings.json" ]; then settings_base=$MAIN_HOME/settings.json
else settings_base=; fi
if [ -n "$TELEMETRY" ]; then
    value=true; [ "$TELEMETRY" = off ] && value=false
    if [ -n "$settings_base" ]; then
        jq "$TELEMETRY_KEY = $value" "$settings_base" > "$STAGE/settings.json" || die 2 "$settings_base is not valid JSON; fix or move it"
    else
        jq -n "{} | $TELEMETRY_KEY = $value" > "$STAGE/settings.json"
    fi
elif [ -n "$settings_base" ]; then
    # The window's own settings.json when it has one (put_file then finds it identical and leaves it alone), else the
    # main home's. Either way it is staged, so the manifest lists it on every run: a rerun with the same options
    # rebuilds the same manifest, and nothing is written.
    cp "$settings_base" "$STAGE/settings.json"
else
    SETTINGS_NOTE="note: a fresh Herdr home turns telemetry on by default; pass --telemetry off to turn it off"
fi

# The theme the staged settings.json names lives in <main home>/themes/<name>.json; the window's home has no such
# folder, so without the file Herdr does not apply the layout. The file is staged like any other (manifest, refusals,
# backup on change). The name becomes a path, so only a plain file name is followed. Herdr's own theme names are not
# listed here: a theme with no file in the main home just gets the note, which is harmless for a built-in one.
THEME_NOTE=
if [ -f "$STAGE/settings.json" ]; then
    theme=$(jq -r 'if (.theme | type) == "string" then .theme else empty end' "$STAGE/settings.json" 2>/dev/null || true)
    if [ -n "$theme" ]; then
        case $theme in
            .|..|*[!A-Za-z0-9._-]*)
                THEME_NOTE="note: settings.json names a theme that is not a plain file name (letters, digits, dot, dash, underscore), so no theme was copied" ;;
            *)
                if [ -f "$MAIN_HOME/themes/$theme.json" ]; then
                    mkdir -p "$STAGE/themes" && cp "$MAIN_HOME/themes/$theme.json" "$STAGE/themes/$theme.json"
                else
                    THEME_NOTE="note: settings.json names the theme '$theme' but $MAIN_HOME/themes/$theme.json was not found, so no theme was copied (harmless for a theme Herdr ships)"
                fi ;;
        esac
    fi
fi

fill_launcher "$HERE/bin/herdr-cao.in" "$WORK/launcher" "$OS" "$APP" "$HOME_DIR" "$CORE" "$COPY" || exit $?

# Manifest: options and the SHA-256 of every file written. No timestamps, so a rerun is identical.
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
# One hashing run and one jq run for every staged file, not one of each per file: the installer is run many times by its tests.
( cd "$STAGE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort ) > "$WORK/hash.list"
( cd "$STAGE" && tr '\n' '\0' < "$WORK/hash.list" | if command -v sha256sum >/dev/null 2>&1; then xargs -0 sha256sum; else xargs -0 shasum -a 256; fi ) > "$WORK/hashes.txt" \
    || die 1 "cannot hash the staged files"
jq -R -c 'capture("^(?<sha>[0-9a-f]{64})  (?<path>.*)$") | {path: .path, sha256: .sha}' "$WORK/hashes.txt" > "$WORK/files.ndjson" \
    || die 1 "cannot read the hashes of the staged files"
jq -n -c --arg path "$BIN_DIR/$LAUNCHER_NAME" --arg sha "$(sha256 "$WORK/launcher")" '{path: $path, sha256: $sha}' >> "$WORK/files.ndjson"
ICON_SHA=''; [ -z "$COPY" ] || ICON_SHA=$(sha256 "$ICON_SRC")
jq -n --arg home "$HOME_DIR" --arg core "$CORE" --arg machine "$MACHINE" --arg caoCore "$CAO_CORE" \
    --arg ssh "$SSH_HOST" --argjson local "$([ "$LOCAL" = 1 ] && echo true || echo false)" \
    --arg bin "$BIN_DIR" --arg launcher "$LAUNCHER_NAME" --arg app "$APP" --argjson props "$PROPS" \
    --arg copy "$COPY" --arg copyVersion "${APP_RAW:-}" --arg iconSha "$ICON_SHA" \
    --slurpfile files "$WORK/files.ndjson" \
    '{home: $home, core: $core, machine: $machine, caoCore: $caoCore, caoSshHost: $ssh, local: $local,
      binDir: $bin, launcher: $launcher, app: $app,
      appCopy: (if $copy == "" then null else {path: $copy, sourceVersion: $copyVersion, iconSha256: $iconSha} end),
      props: $props, files: $files}' > "$STAGE/cao-window-install.json"

# A layout the user customized (it differs from what we derive and from the hash we recorded) is not
# overwritten without --force.
LAYOUT=$HOME_DIR/layouts/cao-window.json
if [ -f "$LAYOUT" ] && ! cmp -s "$LAYOUT" "$STAGE/layouts/cao-window.json"; then
    recorded=''
    [ -f "$HOME_DIR/cao-window-install.json" ] && recorded=$(jq -r '.files[]? | select(.path == "layouts/cao-window.json") | .sha256' "$HOME_DIR/cao-window-install.json" 2>/dev/null || true)
    if [ "$(sha256 "$LAYOUT")" != "$recorded" ] && [ "$FORCE" != 1 ]; then
        die 2 "$LAYOUT was changed since the last install; nothing was written. Re-run with --force to keep it as a .bak file and replace it"
    fi
fi

# --- check every target, still without writing: a folder where a file goes, a file where a folder goes
# or an unwritable folder stops the run here, before any file changes ---
check_target() {   # check_target DST
    ct_d=${1%/*}; [ -n "$ct_d" ] || ct_d=/        # the parent folder, as dirname says it, without starting a process
    while [ ! -d "$ct_d" ]; do
        if [ -e "$ct_d" ] || [ -L "$ct_d" ]; then die 1 "$ct_d exists and is not a folder; nothing was written"; fi
        ct_d=${ct_d%/*}; [ -n "$ct_d" ] || ct_d=/
    done
    { [ -w "$ct_d" ] && [ -x "$ct_d" ]; } || die 1 "cannot write in $ct_d; nothing was written"
    if [ -e "$1" ] || [ -L "$1" ]; then
        [ -f "$1" ] || die 1 "$1 exists and is not a file; nothing was written"
    fi
}
( cd "$STAGE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort ) > "$WORK/stage.list"
while IFS= read -r rel; do check_target "$HOME_DIR/$rel"; done < "$WORK/stage.list"
check_target "$BIN_DIR/$LAUNCHER_NAME"

# --- write phase ---
CHANGED=0
# The app copy first: when it cannot be built nothing else is written. It is built next to its target, on the same
# volume, so the clone is cheap and the move at the end is a rename; the old copy (ours, checked again here) goes only
# when the new one is signed and verified.
if [ "$COPY_BUILD" = 1 ]; then
    mkdir -p -- "$APP_COPY_DIR" || die 1 "cannot create $APP_COPY_DIR; nothing was written"
    APP_NEW=$APP_COPY_DIR/.CAO-new.$$.app
    # cp -c clones on APFS (59 MB shared with the Herdr app, not duplicated). A volume that cannot clone gets a plain copy.
    if ! cp -cR "$APP" "$APP_NEW" 2>/dev/null; then
        rm_app_new; APP_NEW=$APP_COPY_DIR/.CAO-new.$$.app
        cp -R "$APP" "$APP_NEW" || die 1 "cannot copy $APP to $APP_NEW"
    fi
    PLIST=$APP_NEW/Contents/Info.plist
    plist_put "$PLIST" CFBundleName CAO && plist_put "$PLIST" CFBundleDisplayName CAO \
        && plist_put "$PLIST" CFBundleIdentifier dev.herdr.app.cao && plist_put "$PLIST" CFBundleIconFile CAO.icns \
        && plist_put "$PLIST" "$COPY_MARK_KEY" "$APP_RAW" || die 1 "cannot edit $PLIST"
    # An icon named through an asset catalog would win over the file; Herdr 0.1.18 has none, a later one might.
    plistbuddy -c "Delete :CFBundleIconName" "$PLIST" >/dev/null 2>&1 || true
    mkdir -p -- "$APP_NEW/Contents/Resources" && cp -- "$ICON_SRC" "$APP_NEW/Contents/Resources/CAO.icns" || die 1 "cannot put the icon in $APP_NEW"
    # The marker is written before the bundle is signed, so the signature covers it.
    printf '%s\n' "$APP_RAW" > "$APP_NEW/$COPY_MARKER_FILE" || die 1 "cannot mark $APP_NEW as ours"
    # The changed plist breaks the original signature; an ad-hoc one makes the bundle valid again (it has no Team ID, so
    # Herdr's own entitlements and updater trust do not carry over; see the README).
    xattr -cr "$APP_NEW" || die 1 "xattr -cr failed on $APP_NEW"
    codesign --force --deep -s - "$APP_NEW" || die 1 "codesign could not sign $APP_NEW"
    codesign -v "$APP_NEW" || die 1 "codesign could not verify $APP_NEW"
    if [ -e "$COPY" ] || [ -L "$COPY" ]; then
        app_copy_is_ours "$COPY" || die 1 "$COPY is no longer a copy this installer made; it was left in place"
        rm -r -- "$COPY" || die 1 "cannot remove the old $COPY"
    fi
    mv -- "$APP_NEW" "$COPY" || die 1 "cannot move the new copy to $COPY"
    APP_NEW=
    CHANGED=1; echo "install: built $COPY from $APP ($APP_RAW)"
fi
# The window's home is private to its owner whatever the umask: its manifest names the launcher and the core session
# that uninstall acts on. It is made before anything is written into it and fixed on every run, so a home an earlier
# run left readable by others is closed again even when nothing else changes. No "--" after the mode: macOS chmod
# reads it as a file name. HOME_DIR is absolute (canon), so it cannot be read as an option.
mkdir -p -- "$HOME_DIR" && chmod 700 "$HOME_DIR" || die 1 "cannot make $HOME_DIR private (mode 700)"
EPOCH=$(date +%s)
put_file() {   # put_file SRC DST LABEL [BACKUP_BASE]: write DST when it differs, keeping the old one as <base>.bak-<epoch>
    if [ -f "$2" ] && cmp -s "$1" "$2"; then return 0; fi
    [ -d "${2%/*}" ] || mkdir -p -- "${2%/*}" || die 1 "cannot create the folder for $3"
    if [ -f "$2" ]; then
        # A backup never replaces a file: when <base>.bak-<epoch> is taken (a second run in the same second, or
        # a file of the user's) the first free <base>.bak-<epoch>-<n> is used, n from 1. The base is the file
        # itself unless the caller names another one (the launcher's backups stay out of the folder PATH reads).
        bak=${4:-$2}.bak-$EPOCH
        n=0
        while [ -e "$bak" ] || [ -L "$bak" ]; do n=$((n + 1)); bak=${4:-$2}.bak-$EPOCH-$n; done
        [ -d "${bak%/*}" ] || mkdir -p -- "${bak%/*}" || die 1 "cannot create the folder for the old $3"
        cp -p -- "$2" "$bak" || die 1 "cannot keep the old $3"
        if [ -n "${4:-}" ]; then echo "install: kept the old $3 as $bak"; else echo "install: kept the old $3 as $3${bak#"$2"}"; fi
    fi
    # The temp file name carries this process's id; the exit trap removes it if the run stops before the mv.
    OWN_TMP=$2.new.$$
    if ! { cp -- "$1" "$OWN_TMP" && mv -f -- "$OWN_TMP" "$2"; }; then die 1 "cannot write $3"; fi
    OWN_TMP=
    CHANGED=1; echo "install: wrote $3"
}
# The manifest goes last. It records the hash of the layout it came with, so a run that stops before the
# layout is written must still find the old manifest, or the next run would read its own half-written
# state as a layout the user customized.
while IFS= read -r rel; do
    [ "$rel" = cao-window-install.json ] || put_file "$STAGE/$rel" "$HOME_DIR/$rel" "$rel"
done < "$WORK/stage.list"
put_file "$WORK/launcher" "$BIN_DIR/$LAUNCHER_NAME" "$BIN_DIR/$LAUNCHER_NAME" "$HOME_DIR/launcher-backups/$LAUNCHER_NAME"
put_file "$STAGE/cao-window-install.json" "$HOME_DIR/cao-window-install.json" cao-window-install.json

# The CAO machine entry of the window's own core (HC_SESSION=$CORE, never CAO's core or the default one). An entry made for
# another CAO host is replaced, or a rerun with a new --cao-ssh-host (or a switch to or from --local) would record the new
# host in the manifest while the core kept the old one. hc lists a --via machine with via true and its command as target; an
# ssh machine is told apart by its target. hc may write a host differently from how it was typed, so an entry whose target
# differs counts only when the last manifest does not vouch for these options either; with no target to read, the manifest
# alone decides.
MACHINE_ENTRY=$(HC_SESSION=$CORE hc machine ls --json 2>/dev/null | jq -c --arg m "$MACHINE" '[.. | objects | select(.name? == $m)] | first // empty' 2>/dev/null || true)
machine_is_stale() {
    printf '%s' "$MACHINE_ENTRY" | jq -e --argjson local "$([ "$LOCAL" = 1 ] && echo true || echo false)" --arg host "$SSH_HOST" \
        --argjson prev "$(jq -n -c --argjson e "$PREV_EXISTS" --argjson l "$PREV_LOCAL" --arg h "$PREV_HOST" '{exists: $e, local: $l, host: $h}')" '
        ($prev.exists and $prev.local == $local and $prev.host == $host) as $vouched
        | .via as $via | (.target? // null) as $t
        | if $local then
            if $via == true then false
            elif $via == false then true
            elif $t == "sh -c" then false
            else ($prev.exists and ($prev.local | not)) end
          else
            if $via == true then true
            elif ($t | type) == "string" then ($t != $host and ($vouched | not))
            else ($prev.exists and ($vouched | not)) end
          end' >/dev/null 2>&1
}
REPLACED=0
if [ -n "$MACHINE_ENTRY" ] && machine_is_stale; then
    HC_SESSION=$CORE hc machine rm "$MACHINE" \
        || die 1 "hc machine rm $MACHINE failed in core session '$CORE'; the files are written, fix hc and run the installer again"
    MACHINE_ENTRY=''; REPLACED=1
fi
if [ -z "$MACHINE_ENTRY" ]; then
    if [ "$LOCAL" = 1 ]; then
        HC_SESSION=$CORE hc machine add --via "sh -c" --name "$MACHINE" --session "$CAO_CORE" \
            || die 1 "hc machine add failed; the files are written, fix hc and run the installer again"
    else
        HC_SESSION=$CORE hc machine add "$SSH_HOST" --session "$CAO_CORE" --name "$MACHINE" \
            || die 1 "hc machine add failed; the files are written, fix hc and run the installer again"
    fi
    CHANGED=1
    if [ "$REPLACED" = 1 ]; then
        echo "install: replaced machine '$MACHINE' in core session '$CORE': its entry was made for another CAO host, and it now points at $([ "$LOCAL" = 1 ] && echo "this machine (--local)" || echo "$SSH_HOST")"
    else
        echo "install: added machine '$MACHINE' to core session '$CORE'"
    fi
fi

[ -z "$SETTINGS_NOTE" ] || echo "install: $SETTINGS_NOTE"
[ -z "$THEME_NOTE" ] || echo "install: $THEME_NOTE"
echo "install: toast mod $([ "$TOAST" = 1 ] && echo on || echo off), mod imports: $IMPORTS"   # a stray exported variable shows here
if [ "$CHANGED" = 0 ]; then echo "install: up to date"; else echo "install: done. Open the window with: $BIN_DIR/$LAUNCHER_NAME"; fi
