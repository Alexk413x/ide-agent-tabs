echo @'
' > /dev/null
# Sourced, not run, by both sh and PowerShell: the second SessionStart command in hooks/hooks.json dot-sources this
# file. Claude Code runs it in Git Bash or sh, or in PowerShell on Windows without Git Bash. sh reads this part and
# exits before the PowerShell part, which PowerShell reads after skipping this here-string. It runs
# `agent_tabs.py sync-ides --hook` like `agent-tabs sync-ides --hook`, except that without Python it exits 0 and says
# nothing: the setup skill reports a missing Python, and a hook error on every session start would not.
main="$CLAUDE_PLUGIN_ROOT/mcp/launch/agent_tabs.py"
if [ -n "${WINDIR:-}" ] && command -v py >/dev/null 2>&1; then
  exec py -3 -I -S "$main" sync-ides --hook
fi
for name in python3 python; do
  found=$(command -v "$name" 2>/dev/null) || continue
  case "$found" in
    *WindowsApps*) continue ;;
  esac
  exec "$found" -I -S "$main" sync-ides --hook
done
exit 0
'@ > $null
$main = Join-Path $PSScriptRoot 'agent_tabs.py'
$py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($py) {
  & $py.Source -3 -I -S $main sync-ides --hook
  exit $LASTEXITCODE
}
foreach ($name in 'python3.exe', 'python.exe') {
  $found = Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1
  if ($found) {
    & $found.Source -I -S $main sync-ides --hook
    exit $LASTEXITCODE
  }
}
exit 0
