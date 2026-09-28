# Sourced by bash or zsh. IDE_AGENT_TABS_SPEC names a NUL-separated launch spec written by the MCP server.
__ide_agent_tabs_launch() {
    [ -n "${ZSH_VERSION-}" ] && emulate -L zsh
    local spec="${IDE_AGENT_TABS_SPEC-}" f c cwd n
    local -a fields a
    unset IDE_AGENT_TABS_SPEC IDE_AGENT_TABS_LAUNCHER
    if [ ! -f "$spec" ]; then
        printf 'ide-agent-tabs: launch spec not found\n' >&2
        return 1
    fi
    while IFS= read -r -d '' f; do
        fields+=("$f")
    done < "$spec"
    rm -f -- "$spec"
    set -- "${fields[@]}"
    if [ "$#" -lt 7 ] || [ "$1" != "ide-agent-tabs-spec-1" ]; then
        printf 'ide-agent-tabs: unreadable launch spec\n' >&2
        return 1
    fi
    export IDE_AGENT_TABS_ID="$2" IDE_AGENT_TABS_AGENT="$3"
    cwd="$4"
    c="$5"
    n="$6"
    shift 6
    while [ "$n" -gt 0 ]; do
        export "$1=$2"
        shift 2
        n=$((n - 1))
    done
    n="$1"
    shift
    while [ "$n" -gt 0 ]; do
        a+=("$1")
        shift
        n=$((n - 1))
    done
    if [ "$1" = 1 ]; then
        a+=("$2")
        shift 2
    else
        shift
    fi
    [ -n "${1-}" ] && printf '%s\n' "$$" > "$1"
    cd -- "$cwd" || return 1
    # bash 3.2 (macOS) treats an empty "${a[@]}" as unset under set -u.
    "$c" ${a[@]+"${a[@]}"}
}
__ide_agent_tabs_launch
unset -f __ide_agent_tabs_launch
