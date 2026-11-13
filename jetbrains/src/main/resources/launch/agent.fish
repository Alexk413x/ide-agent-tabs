function __ide_agent_tabs
    set -l c $IDE_AGENT_TABS_COMMAND
    set -e IDE_AGENT_TABS_COMMAND
    set -l a
    set -l n 0
    # A variable that is set but empty counts as unset, so a launcher that cannot remove one can empty it.
    set -q IDE_AGENT_TABS_ARGC; and test -n "$IDE_AGENT_TABS_ARGC"; and set n $IDE_AGENT_TABS_ARGC
    set -l i 0
    while test $i -lt $n
        set -l name IDE_AGENT_TABS_ARG_$i
        set -a a "$$name"
        set -e $name
        set i (math $i + 1)
    end
    set -e IDE_AGENT_TABS_ARGC
    test -n "$IDE_AGENT_TABS_PROMPT"; and set -a a "$IDE_AGENT_TABS_PROMPT"
    set -e IDE_AGENT_TABS_PROMPT
    $c $a
end
__ide_agent_tabs
functions -e __ide_agent_tabs
