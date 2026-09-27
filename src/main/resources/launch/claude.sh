# Sourced by the terminal's bash or zsh integration through JEDITERM_SOURCE, after the user's startup files.
__claude_studio_tabs() {
    [ -n "${ZSH_VERSION-}" ] && emulate -L zsh
    local -a a
    local i=0 v
    while [ "$i" -lt "${CLAUDE_STUDIO_TABS_ARGC:-0}" ]; do
        eval "v=\${CLAUDE_STUDIO_TABS_ARG_$i}"
        a+=("$v")
        unset "CLAUDE_STUDIO_TABS_ARG_$i"
        i=$((i + 1))
    done
    unset CLAUDE_STUDIO_TABS_ARGC
    if [ -n "${CLAUDE_STUDIO_TABS_PROMPT+set}" ]; then
        a+=("$CLAUDE_STUDIO_TABS_PROMPT")
        unset CLAUDE_STUDIO_TABS_PROMPT
    fi
    # bash 3.2 (macOS) treats an empty "${a[@]}" as unset under set -u.
    claude ${a[@]+"${a[@]}"}
}
__claude_studio_tabs
unset -f __claude_studio_tabs
