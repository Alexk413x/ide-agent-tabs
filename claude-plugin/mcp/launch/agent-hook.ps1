echo @'
' > /dev/null
# Sourced, not run, by both sh and PowerShell: each hook command in hooks/hooks.json runs `set -- claude <event>` and
# dot-sources this file. Claude Code runs it in Git Bash or sh, or in PowerShell on Windows without Git Bash. sh reads
# this part and exits before the PowerShell part, which PowerShell reads after skipping this here-string.
[ -n "${IDE_AGENT_TABS_ID:-}" ] && [ "$IDE_AGENT_TABS_ID" != "${IDE_AGENT_TABS_MOD:-}" ] || exit 0
hook="$CLAUDE_PLUGIN_ROOT/mcp/launch/agent_hook.py"
cache="${IDE_AGENT_TABS_HOME:-$HOME/.ide-agent-tabs}/mcp/hook-python"
if { read -r python < "$cache"; } 2>/dev/null && [ -f "$python" ]; then
  exec "$python" -I -S "$hook" "$@"
fi
export IDE_AGENT_TABS_HOOK_PYTHON="$cache"
if [ -n "${WINDIR:-}" ] && command -v py >/dev/null 2>&1; then
  exec py -3 -I -S "$hook" "$@"
fi
for name in python3 python; do
  found=$(command -v "$name" 2>/dev/null) || continue
  case "$found" in
    *WindowsApps*) continue ;;
  esac
  exec "$found" -I -S "$hook" "$@"
done
exit 0
'@ > $null
if (-not $env:IDE_AGENT_TABS_ID -or $env:IDE_AGENT_TABS_ID -eq $env:IDE_AGENT_TABS_MOD) { exit 0 }
$hook = Join-Path $PSScriptRoot 'agent_hook.py'
$base = if ($env:IDE_AGENT_TABS_HOME) { $env:IDE_AGENT_TABS_HOME } else { Join-Path $HOME '.ide-agent-tabs' }
$cache = Join-Path (Join-Path $base 'mcp') 'hook-python'
$python = if (Test-Path -LiteralPath $cache) { Get-Content -LiteralPath $cache -TotalCount 1 }
if ($python -and (Test-Path -LiteralPath $python -PathType Leaf)) {
  & $python -I -S $hook claude $claude
  exit 0
}
$env:IDE_AGENT_TABS_HOOK_PYTHON = $cache
$py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($py) {
  & $py.Source -3 -I -S $hook claude $claude
  exit 0
}
foreach ($name in 'python3.exe', 'python.exe') {
  $found = Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1
  if ($found) {
    & $found.Source -I -S $hook claude $claude
    exit 0
  }
}
exit 0
