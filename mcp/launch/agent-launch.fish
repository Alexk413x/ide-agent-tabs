# Sourced by fish. IDE_AGENT_TABS_SPEC names a NUL-separated launch spec written by the MCP server.
function __ide_agent_tabs_launch
    set -l spec "$IDE_AGENT_TABS_SPEC"
    set -e IDE_AGENT_TABS_SPEC
    set -e IDE_AGENT_TABS_LAUNCHER
    if not test -f "$spec"
        echo 'ide-agent-tabs: launch spec not found' >&2
        return 1
    end
    set -l f (string split0 < $spec)
    rm -f -- $spec
    if test (count $f) -lt 7; or test "$f[1]" != ide-agent-tabs-spec-1
        echo 'ide-agent-tabs: unreadable launch spec' >&2
        return 1
    end
    set -gx IDE_AGENT_TABS_ID $f[2]
    set -gx IDE_AGENT_TABS_AGENT $f[3]
    set -l cwd $f[4]
    set -l c $f[5]
    set -l n $f[6]
    set -l i 7
    while test $n -gt 0
        set -gx $f[$i] $f[(math $i + 1)]
        set i (math $i + 2)
        set n (math $n - 1)
    end
    set n $f[$i]
    set i (math $i + 1)
    set -l a
    while test $n -gt 0
        set -a a $f[$i]
        set i (math $i + 1)
        set n (math $n - 1)
    end
    if test "$f[$i]" = 1
        set -a a $f[(math $i + 1)]
    end
    cd $cwd; or return 1
    $c $a
end
__ide_agent_tabs_launch
functions -e __ide_agent_tabs_launch
