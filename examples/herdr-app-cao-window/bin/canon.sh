# shellcheck shell=sh
# Sourced by install.sh and uninstall.sh; defines canon and contains.
#
# canon PATH: print the absolute form of PATH with symlinks, "." and ".." resolved, whether or not
# PATH exists yet. The components are walked left to right: while they exist (as folders) they are
# resolved with cd -P, a symlink that dangles is followed through readlink, and from the first missing
# component on the rest is folded lexically. So ".../missing/../herdr-app" is the same path as
# ".../herdr-app", and a --home can be compared with the main home however it is spelled.
canon() {
    c_in=$1
    c_depth=${2:-0}
    case $c_in in /*) ;; *) c_in=$PWD/$c_in ;; esac
    c_cur=/
    c_tail=
    c_rest=${c_in#/}
    while [ -n "$c_rest" ]; do
        case $c_rest in
            */*) c_comp=${c_rest%%/*}; c_rest=${c_rest#*/} ;;
            *) c_comp=$c_rest; c_rest= ;;
        esac
        case $c_comp in
            ''|.) ;;
            ..)
                if [ -n "$c_tail" ]; then
                    case $c_tail in */*) c_tail=${c_tail%/*} ;; *) c_tail= ;; esac
                else
                    c_cur=$(cd -P -- "${c_cur%/}/.." && pwd -P)
                fi
                ;;
            *)
                if [ -z "$c_tail" ] && [ -d "${c_cur%/}/$c_comp" ]; then
                    c_cur=$(cd -P -- "${c_cur%/}/$c_comp" && pwd -P)
                elif [ -z "$c_tail" ] && [ -L "${c_cur%/}/$c_comp" ] && [ "$c_depth" -lt 20 ] \
                    && c_link=$(readlink -- "${c_cur%/}/$c_comp" 2>/dev/null) && [ -n "$c_link" ]; then
                    case $c_link in /*) ;; *) c_link=${c_cur%/}/$c_link ;; esac
                    canon "$c_link${c_rest:+/$c_rest}" $((c_depth + 1))
                    return
                else
                    c_tail=${c_tail:+$c_tail/}$c_comp
                fi
                ;;
        esac
    done
    if [ -z "$c_tail" ]; then printf '%s\n' "$c_cur"
    elif [ "$c_cur" = / ]; then printf '/%s\n' "$c_tail"
    else printf '%s/%s\n' "$c_cur" "$c_tail"; fi
}

# contains OUTER INNER: true when INNER is OUTER or lies below it. Both are canon forms.
contains() {
    [ "$1" = "$2" ] && return 0
    [ "$1" = / ] && return 0
    case $2 in "$1"/*) return 0 ;; esac
    return 1
}
