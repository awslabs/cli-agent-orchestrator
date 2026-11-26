# shellcheck shell=sh
# Sourced by install.sh and uninstall.sh; defines the reading and writing of the app copy's Info.plist.
#
# The app copy (CAO.app) is a renamed clone of the Herdr app. Two things mark it as ours: the plist key
# below, which holds the Herdr version the copy was made from and tells install.sh whether to rebuild it,
# and a marker file in the bundle, which an update that rewrites Info.plist in place leaves behind.
# install.sh refuses to replace, and uninstall.sh refuses to remove, a CAO.app folder with neither.
COPY_MARK_KEY=CAOWindowSourceVersion
COPY_MARKER_FILE=Contents/Resources/cao-window-copy

# PlistBuddy lives in /usr/libexec, which is not on PATH; a PlistBuddy on PATH wins (the tests put a stub there).
plistbuddy() {
    if command -v PlistBuddy >/dev/null 2>&1; then PlistBuddy "$@"; else /usr/libexec/PlistBuddy "$@"; fi
}
plist_has_tool() { command -v PlistBuddy >/dev/null 2>&1 || [ -x /usr/libexec/PlistBuddy ]; }

# plist_get FILE KEY: print the value of a top-level key; fails when the file or the key is missing.
plist_get() { plistbuddy -c "Print :$2" "$1" 2>/dev/null; }

# plist_put FILE KEY VALUE: set a top-level string key, adding it when the file has none. One command per call.
plist_put() {
    plistbuddy -c "Set :$2 $3" "$1" >/dev/null 2>&1 || plistbuddy -c "Add :$2 string $3" "$1" >/dev/null
}

# app_copy_mark DIR: prints the Herdr version recorded in DIR when DIR is a real folder (not a link) whose
# Contents/Info.plist carries our key; fails for anything else.
app_copy_mark() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 1
    ac_v=$(plist_get "$1/Contents/Info.plist" "$COPY_MARK_KEY") || return 1
    [ -n "$ac_v" ] || return 1
    printf '%s\n' "$ac_v"
}

# app_copy_is_ours DIR: true for a real folder (not a link) that is a copy install.sh made: its Info.plist
# carries our key, or the bundle holds our marker file (a regular file). The marker is what keeps a copy that
# the Herdr updater changed in place (new Info.plist, no key) ours, so a rerun can rebuild it and uninstall can
# remove it, while any other folder named CAO.app is still left alone.
app_copy_is_ours() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 1
    app_copy_mark "$1" >/dev/null 2>&1 && return 0
    [ -f "$1/$COPY_MARKER_FILE" ] && [ ! -L "$1/$COPY_MARKER_FILE" ]
}
