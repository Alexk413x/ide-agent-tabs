# Sourced, not run: the SessionStart and SessionEnd entries in hooks/hooks.json set the event with `set --` and source this file.
hook="$CLAUDE_PLUGIN_ROOT/mcp/launch/server_hook.py"
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
