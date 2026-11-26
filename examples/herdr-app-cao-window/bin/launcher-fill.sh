# shellcheck shell=sh
# Sourced by install.sh and launcher.test.mjs; defines fill_launcher.
# Set to 1 only once a released Herdr app has been shown to run on Linux.
# That check has not been done (no Linux machine with a display), so the installer exits 4 on Linux.
LINUX_SUPPORTED=0

# Print $1 quoted for sh only when it needs quoting. Pure shell (no sed), so a value with a newline
# or a trailing newline survives unchanged.
sq() {
    case $1 in
        ''|*[!A-Za-z0-9_./:@%+=,-]*)
            sq_rest=$1
            sq_out=
            while :; do
                case $sq_rest in
                    *\'*)
                        sq_out=$sq_out${sq_rest%%\'*}"'\\''"
                        sq_rest=${sq_rest#*\'}
                        ;;
                    *)
                        sq_out=$sq_out$sq_rest
                        break
                        ;;
                esac
            done
            printf "'%s'" "$sq_out"
            ;;
        *) printf %s "$1" ;;
    esac
}

# fill_launcher TEMPLATE OUT OS APP HOME CORE [COPY]
# Writes OUT (mode 755) from TEMPLATE, or returns 4 with a reason on stderr and writes nothing.
# COPY is the path of the app copy (CAO.app, made by install.sh) on macOS: the launcher opens it while it
# is a folder and opens APP when it is not. Empty or absent: the launcher opens APP only. Linux ignores it.
fill_launcher() {
    fl_template=$1 fl_out=$2 fl_os=$3 fl_app=$4 fl_home=$5 fl_core=$6 fl_copy=${7:-}
    case $fl_os in
        Darwin)
            fl_env="--env HERDR_APP_HOME=$(sq "$fl_home") --env HC_SESSION=$(sq "$fl_core")"
            fl_line="exec open -n -a $(sq "$fl_app") $fl_env"
            if [ -n "$fl_copy" ]; then
                fl_line="if [ -d $(sq "$fl_copy") ]; then
    exec open -n -a $(sq "$fl_copy") $fl_env
fi
$fl_line"
            fi
            ;;
        Linux)
            if [ "$LINUX_SUPPORTED" != 1 ]; then
                echo "install: the Herdr app on Linux is unsupported (see README, topology: Linux)" >&2
                return 4
            fi
            fl_line="env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE -u HC_SOCKET HERDR_APP_HOME=$(sq "$fl_home") HC_SESSION=$(sq "$fl_core") $(sq "$fl_app") >/dev/null 2>&1 &"
            ;;
        *)
            echo "install: no Herdr app is supported on '$fl_os' (see README, topologies)" >&2
            return 4
            ;;
    esac
    fl_tmp="$fl_out.tmp.$$"
    if ! {
        while IFS= read -r fl_row || [ -n "$fl_row" ]; do
            case $fl_row in
                @@LAUNCH@@) printf '%s\n' "$fl_line" ;;
                *) printf '%s\n' "$fl_row" ;;
            esac
        done < "$fl_template" > "$fl_tmp" && chmod 755 "$fl_tmp" && mv "$fl_tmp" "$fl_out"
    }; then
        rm -f "$fl_tmp"
        echo "install: could not write the launcher $fl_out" >&2
        return 1
    fi
}
