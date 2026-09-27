# Sourced by the terminal's fish integration through JEDITERM_SOURCE.
function __claude_studio_tabs
    set -l a
    set -l n 0
    set -q CLAUDE_STUDIO_TABS_ARGC; and set n $CLAUDE_STUDIO_TABS_ARGC
    set -l i 0
    while test $i -lt $n
        set -l name CLAUDE_STUDIO_TABS_ARG_$i
        set -a a "$$name"
        set -e $name
        set i (math $i + 1)
    end
    set -e CLAUDE_STUDIO_TABS_ARGC
    if set -q CLAUDE_STUDIO_TABS_PROMPT
        set -a a "$CLAUDE_STUDIO_TABS_PROMPT"
        set -e CLAUDE_STUDIO_TABS_PROMPT
    end
    claude $a
end
__claude_studio_tabs
functions -e __claude_studio_tabs
