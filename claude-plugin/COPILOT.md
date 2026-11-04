# Agent Tabs for Copilot CLI

Use Agent Tabs from Copilot CLI without starting Claude Code. This integration adds a Copilot MCP
registration and Copilot-specific messaging hooks. It does not replace Claude Code or Codex manifests,
hooks, skills, or UI.

## Set up from a local plugin folder

Install Python 3.9 or later and Copilot CLI. Use a CLI version that supports direct executable hooks
(`exec` and `args`) and user-level hooks. This setup path does not need Git Bash on Windows.

Locate the plugin folder. In a development checkout it is `claude-plugin`. In the flattened `release`
branch and marketplace installation, it is the installation root. Both contain
`mcp/launch/agent_tabs.py` and the Python package. Keep this folder to refresh the server after updates.

For Windows PowerShell, replace the path below with that folder:

```powershell
$plugin = 'C:\path\to\plugin'
py -3 -I -S "$plugin\mcp\launch\agent_tabs.py" sync-ides --agents
py -3 -I -S "$plugin\mcp\launch\agent_tabs.py" sync-ides --register copilot
```

For macOS or Linux:

```sh
plugin='/path/to/plugin'
python3 -I -S "$plugin/mcp/launch/agent_tabs.py" sync-ides --agents
python3 -I -S "$plugin/mcp/launch/agent_tabs.py" sync-ides --register copilot
```

`--agents` inspects configuration. `--register copilot` writes the `ide-agent-tabs` entry in
`$COPILOT_HOME/mcp-config.json` and hooks in `$COPILOT_HOME/hooks/ide-agent-tabs.json`.
If `COPILOT_HOME` is unset, it uses `~/.copilot`. It preserves other MCP entries and unrelated hook
handlers. It does not grant tool permissions.

The command copies the server to `~/.ide-agent-tabs/mcp/py` and records an absolute Python interpreter
path. Set `IDE_AGENT_TABS_HOME` before registration to select another data folder. Both the Copilot MCP
process and its hooks receive that folder explicitly. Hook commands pass paths as argument arrays,
including Windows paths with spaces, rather than through a shell.

For an isolated check, set both `COPILOT_HOME` and `IDE_AGENT_TABS_HOME` to scratch directories before
registration. Run Copilot with those same variables. Do not copy credentials into the scratch folder.

Restart open Copilot CLI sessions after registration. Run `copilot mcp get ide-agent-tabs` from outside
a repository with an overriding MCP entry to inspect the effective registration. In a signed-in local
CLI session, call `list_agents`, `list_tabs`, and `list_sessions`. Send a message only to a consenting
test session, then read it there with `read_messages`.

IDE tabs require the Agent Tabs IDE extension. To inspect bundled extension status without installing:

```powershell
py -3 -I -S "$plugin\mcp\launch\agent_tabs.py" sync-ides --status
```

Install an extension only after approving that separate change. Claude Code's setup remains available
and unchanged for Claude users.

## Updates, restarts, and removal

After updating the plugin folder, rerun `sync-ides --register copilot` from that folder and restart
Copilot CLI. Registration is idempotent and updates legacy Agent Tabs hook commands without removing
unrelated handlers. Stable server paths select the refreshed build.

Registered standalone Copilot CLI sessions use stdio and do not require Claude's shared HTTP server.
Agent Tabs can select its shared HTTP server for a Copilot tab when the recorded port, generated HTTP
configuration, registered hooks, and server health agree. Otherwise, the tab relies on its registered
stdio server. If an HTTP-backed session loses its server, restart that session; do not assume the CLI
automatically switches transport during a session.

To remove only Copilot's registration, use the same Python entry point with
`sync-ides --unregister copilot`. This removes Agent Tabs hooks but retains unrelated handlers and
metadata in the hooks file. Do not delete the shared data folder while another client uses it.

## Support boundaries

Messaging hooks track sessions inside Agent Tabs tabs, where `IDE_AGENT_TABS_ID` identifies the tab.
Outside a tab, hooks return without changing tab state. Agents still use the MCP messaging tools.
Disabled hooks, workspace trust, enterprise policies, and sandbox access can restrict integration.
Do not disable those controls to make a check pass.

CLI checks do not establish support for the Copilot desktop app, VS Code chat, or cloud agent.
Those surfaces have different configuration, lifecycle, and filesystem access. A cloud sandbox cannot
control IDEs on your local machine through this loopback server. This integration does not port
Claude Code's mod UI.

References:

- [Copilot CLI MCP configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
- [Copilot hook formats and lifecycle](https://docs.github.com/en/copilot/reference/hooks-reference)
- [Copilot configuration directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
