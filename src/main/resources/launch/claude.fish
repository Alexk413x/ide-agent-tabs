# Sourced by the terminal's fish integration through JEDITERM_SOURCE.
function __ide_agent_tabs
    set -l a
    set -l n 0
    set -q IDE_AGENT_TABS_ARGC; and set n $IDE_AGENT_TABS_ARGC
    set -l i 0
    while test $i -lt $n
        set -l name IDE_AGENT_TABS_ARG_$i
        set -a a "$$name"
        set -e $name
        set i (math $i + 1)
    end
    set -e IDE_AGENT_TABS_ARGC
    if set -q IDE_AGENT_TABS_PROMPT
        set -a a "$IDE_AGENT_TABS_PROMPT"
        set -e IDE_AGENT_TABS_PROMPT
    end
    claude $a
end
__ide_agent_tabs
functions -e __ide_agent_tabs
