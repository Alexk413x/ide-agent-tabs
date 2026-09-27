# Sourced by the terminal's bash or zsh integration through JEDITERM_SOURCE, after the user's startup files.
__ide_agent_tabs() {
    [ -n "${ZSH_VERSION-}" ] && emulate -L zsh
    local -a a
    local i=0 v
    while [ "$i" -lt "${IDE_AGENT_TABS_ARGC:-0}" ]; do
        eval "v=\${IDE_AGENT_TABS_ARG_$i}"
        a+=("$v")
        unset "IDE_AGENT_TABS_ARG_$i"
        i=$((i + 1))
    done
    unset IDE_AGENT_TABS_ARGC
    if [ -n "${IDE_AGENT_TABS_PROMPT+set}" ]; then
        a+=("$IDE_AGENT_TABS_PROMPT")
        unset IDE_AGENT_TABS_PROMPT
    fi
    # bash 3.2 (macOS) treats an empty "${a[@]}" as unset under set -u.
    claude ${a[@]+"${a[@]}"}
}
__ide_agent_tabs
unset -f __ide_agent_tabs
