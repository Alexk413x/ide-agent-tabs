# Agent Tabs for JetBrains IDEs

A plugin for IntelliJ IDEA, Android Studio and other JetBrains IDEs that opens AI coding-agent sessions,
such as Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi, Hermes, OpenCode,
Qwen Code and Goose, in editor tabs.

- Click **New Agent Tab** in the main toolbar or the **Tools** menu, or press **Ctrl+Alt+A** (**⌘⌥A** on
  macOS), to start the default agent in the project root.
- Right-click **New Agent Tab** in the main toolbar, or open **Tools > New Agent Tab With**, to open
  another installed agent. Opening an agent from a menu doesn't change the default.
- Open **Settings > Tools > Agent Tabs** to change the default agent.
- The default agent can open by itself when a project opens. See [Open on startup](#open-on-startup).
- Other programs, such as another agent session, can open, list and close tabs through a local HTTP
  API. The new tab takes focus only when the request asks for it.

## Requirements

- IntelliJ IDEA, Android Studio or another JetBrains IDE at build 262.10315 or later (2026.2.2).
- At least one agent CLI on your `PATH`, such as [Claude Code](https://docs.claude.com/en/docs/claude-code)
  (`claude`).
- Terminal shell integration turned on. It is on by default: **Settings > Tools > Terminal > Shell
  integration**.

The plugin starts the agent in these shells:

| OS | Shell |
|---|---|
| Windows | PowerShell 7 (`pwsh`), or Windows PowerShell when `pwsh` isn't installed |
| macOS, Linux | Your login shell (`$SHELL`) when it is bash, zsh, fish or `pwsh`; otherwise bash |

## Install

Install the Agent Tabs Claude Code plugin, then run its setup skill. The Claude Code plugin carries this
plugin and keeps it up to date through a local plugin repository:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

The repository is private, so installing needs read access to it: sign in to GitHub with an account
that has access, for example with `gh auth login`.

In a Claude Code session, run `/ide-agent-tabs:setup`. For each JetBrains IDE, the skill gives you two
one-time steps: add a local plugin repository in **Settings > Plugins > ⚙ > Manage Plugin
Repositories**, then install **Agent Tabs** from the **Marketplace** tab. Each later version arrives as a
normal plugin update.

To install by hand instead:

1. Get the plugin zip: `claude-plugin/dist/ide/ide-agent-tabs-jetbrains.zip` in the repository. You can also build it (see [Build from source](#build-from-source)).
2. In the IDE, open **Settings > Plugins**, click **⚙**, and choose **Install Plugin from Disk**.
3. Select the zip file, then restart the IDE.

A plugin installed by hand doesn't update by itself.

## Agents

The plugin knows these agents:

| Name | Label | Command | First prompt |
|---|---|---|---|
| `claude` | Claude Code | `claude` | positional |
| `codex` | Codex | `codex --no-daemon` and `-c` options that add Agent Tabs messaging; needs Codex 0.158 or later | positional |
| `agy` | Antigravity CLI | `agy` | `-i <prompt>` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` |
| `grok` | Grok Build | `grok` | positional |
| `pi` | Pi | `pi` | positional |
| `hermes` | Hermes | `hermes chat` | `-q <prompt>` |
| `opencode` | OpenCode | `opencode` | `--prompt <prompt>` |
| `qwen` | Qwen Code | `qwen` | `-i <prompt>` |
| `goose` | Goose | `goose run -s`; needs a first prompt to start | `-t <prompt>` |
| `codex-local` | Codex (local) | `codex` with the Agent Tabs options, `--oss` and `--local-provider ollama` | positional |

The Grok Build, Pi, Hermes, OpenCode, Qwen Code, Goose and Codex (local) profiles come from each CLI's
documentation and haven't had a live test.

The menus list only agents whose command is on the IDE's `PATH`. On Windows, the plugin
also looks for `.exe`, `.cmd`, `.bat` and `.ps1` files.

To add an agent or change a built-in one, create `~/.ide-agent-tabs/agents.json`. A profile with the
same name as a built-in one replaces it.

```json
{
  "opencode-local": {
    "label": "OpenCode (local model)",
    "command": "opencode",
    "args": ["--model", "<provider>/<model>"],
    "promptFlag": "--prompt",
    "env": {},
    "icon": "icons/opencode.svg"
  }
}
```

| Field | Required | Description |
|---|---|---|
| `command` | Yes | The executable, found on the shell's `PATH`, or an absolute path. |
| `label` | No | The name in menus. The default is the profile name. |
| `args` | No | Arguments that go before the caller's `args`. |
| `promptFlag` | No | The flag before the first prompt. Leave it out when the prompt is positional. |
| `modelFlag` | No | The option that picks a model, such as `--model`. A request's `model` field needs it, unless the tab starts through Ori. The built-in agents set it. |
| `env` | No | Environment variables for the session. The caller's `env` wins on a clash. |
| `icon` | No | An SVG file for menus, as an absolute path or relative to `~/.ide-agent-tabs`. |

If `agents.json` isn't valid, the plugin logs a warning to `idea.log` and uses only the built-in
agents.

The default agent is in `~/.ide-agent-tabs/config.json`. Every IDE with Agent Tabs reads it, so a change
in one IDE applies to all of them. The settings page writes it:

```json
{ "defaultAgent": "claude" }
```

## Settings

Open **Settings > Tools > Agent Tabs**.

| Setting | Description |
|---|---|
| Default agent | The agent that **New Agent Tab** opens. Saved in `~/.ide-agent-tabs/config.json`. |
| Launch through OpenRouter (Ori) | `direct` or `ori`. With `ori`, supported agents start as `ori <agent>`, which bills model usage through OpenRouter, and the menus and tab names carry "via OpenRouter". An agent that Ori can't launch starts directly. Shown only when Ori (`ori`) is installed. Saved as `launchVia`. |
| Close the old tab after a handoff | After a handoff, the new session closes the old tab once both sides confirm. Off leaves the old tab open, marked as handed off. Saved as `closeAfterHandoff`, written only when off. |
| Use the Claude Code mod (in-process messaging) | Claude Code sessions message other agents with SendMessage and ListAgents and get their messages in-process. Off uses the Agent Tabs hooks, wake lines and messaging tools, as in 0.6.0. Claude Code sessions that start after the change pick it up. Saved as `claudeMod`, written only when off. |
| Open on startup | When to open the default agent as a project opens. See [Open on startup](#open-on-startup). |

The page has two more groups. Their settings are shared with VS Code and the MCP server, saved in
`~/.ide-agent-tabs/config.json`. A tab request that names an IDE, a terminal or an agent overrides them.
The choices in the terminal and shell lists come from `~/.ide-agent-tabs/detected.json`, which the MCP
server writes. Without that file, only **Automatic** is listed, plus any value already saved.

**IDE tabs**

| Setting | Key | Description |
|---|---|---|
| Open new tabs in | `tabRouting` | Where a new agent tab opens when no IDE or terminal is named. `project` (**IDE that has the project open**, the default) or `caller` (**IDE the request came from**). |
| Bring new agent tabs to the front | `focusNewTabs` | Whether a tab that an agent opens takes focus. `auto` (**When you asked for the tab**, the default): only when the agent says you asked for it. `always` or `never`. A request's `focus` field wins. The **New Agent Tab** button always brings its tab to the front. |

**Terminal tabs**

| Setting | Key | Description |
|---|---|---|
| Preferred terminal | `terminal` | Terminal for agent tabs when no IDE is running or a terminal is asked for. **Automatic** or a detected terminal. |
| Shell (Windows) | `shell` | PowerShell that runs agent tabs in a terminal. **Automatic**, a detected PowerShell, or **Custom path…**. Windows only. |
| Terminal window | `terminalWindow` | Whether terminal tabs join your last window or a window kept for Agent Tabs. `last` (**Use my last window**, the default) or `dedicated` (**A dedicated Agent Tabs window**). |

### Open on startup

When a project opens, the plugin can open the default agent in an editor tab, in the project root.
Choose one of these values:

| Value | Opens the default agent |
|---|---|
| When the project has a .claude folder | When the project's root folder has a `.claude` folder. This is the default. |
| Always | Every time a project opens. |
| Never | Never. |

Each IDE keeps its own **Open on startup** value. It isn't shared through `config.json`.

## Open a tab from another program

When the IDE starts, the plugin writes a registry file to
`~/.ide-agent-tabs/endpoints/jetbrains-<pid>.json`, and deletes it when the IDE closes:

```json
{"protocol":1,"ide":"jetbrains","product":"IntelliJ IDEA","version":"2026.2.2","pid":12345,
 "url":"http://127.0.0.1:63342/ide-agent-tabs","token":"<64 hex characters>"}
```

Each running IDE writes its own file. A file can stay behind after a crash, so skip any file whose
`pid` isn't running.

Send each request as a `POST` to `<url>/<route>` with `Content-Type: application/json` and
`Authorization: Bearer <token>`. The token changes each time the IDE starts. Every reply is JSON with
`"ok": true`, or `"ok": false` and an `"error"`.

macOS and Linux (uses `jq`, and picks the newest registry file):

```sh
entry=$(ls -t ~/.ide-agent-tabs/endpoints/jetbrains-*.json | head -n 1)
url=$(jq -r .url "$entry")
token=$(jq -r .token "$entry")
curl -s "$url/open" -H 'Content-Type: application/json' -H "Authorization: Bearer $token" \
  -d '{"path": "/path/to/repo", "prompt": "Your first message"}'
```

Windows PowerShell:

```powershell
$entry = Get-ChildItem "$HOME\.ide-agent-tabs\endpoints\jetbrains-*.json" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content -Raw | ConvertFrom-Json
$body = @{ path = 'C:\path\to\repo'; prompt = 'Your first message' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$($entry.url)/open" -ContentType 'application/json' `
  -Headers @{ Authorization = "Bearer $($entry.token)" } -Body $body -UserAgent 'ide-agent-tabs'
```

In PowerShell, always pass `-UserAgent`. PowerShell's default User-Agent starts with `Mozilla/5.0`, and
the IDE refuses a browser-like `POST` before the plugin sees it. `curl` doesn't have this problem.

### Open

`POST` to `open`. The body takes these fields:

| Field | Required | Description |
|---|---|---|
| `path` | Yes | Absolute path to an existing folder. The session starts there. |
| `agent` | No | An agent name from `agents`. The default is the default agent. |
| `prompt` | No | The session's first message. Up to 30,000 characters. |
| `args` | No | Extra agent arguments, such as `["--plugin-dir", "/path/to/plugin"]`. Up to 64 strings. They go after the profile's `args` and before the prompt. |
| `env` | No | Environment variables for the session, such as `{"MY_SETTING": "value"}`. Up to 64. Names that start with `IDE_AGENT_TABS_` or `JEDITERM_SOURCE` are refused. |
| `focus` | No | `true` gives the new editor tab keyboard focus. `false` or absent keeps focus where it is; the new tab still becomes the selected editor tab. |

The reply holds the tab's `id`, the `agent`, the `project` window it opened in, and the `path`. The tab
opens in the open project that contains `path`, or in the last focused project window if none does.

### Close

`POST {"id": "<tab id>"}` to `close`. Closing the tab ends its session. You can close only tabs this
plugin opened. Each session can read its own id from the `IDE_AGENT_TABS_ID` environment variable,
so a session can close its own tab when it finishes. `IDE_AGENT_TABS_AGENT` holds the agent name.

### Input

`POST {"id": "<tab id>", "text": "<one line>"}` to `input`. The plugin types `text` into the tab's
terminal and presses Enter, as if you typed it. `text` is one line of up to 500 characters with no
control characters. The reply holds the tab's `id`. The MCP server uses `input` only to wake an idle
session for a new message.

### List

`POST {}` to `list`. The reply's `tabs` array holds the `id`, `agent`, `project` and `path` of each
open tab the plugin opened.

### Agents

`POST {}` to `agents`. The reply holds the `default` agent name, and an `agents` array with the
`name`, `label`, `command` and `installed` of each agent.

### Info

`POST {}` to `info`. The reply holds the IDE's `ide`, `product`, `version` and `pid`, and a `projects`
array with the `name`, `path` and `focused` of each open project. `focused` is `true` for the project
in the last focused window.

### Status codes

| Status | Meaning |
|---|---|
| 200 | Done. |
| 400 | Bad body, relative path, missing folder, missing `id`, unknown `agent`, or bad `text` for `input`. |
| 401 | Missing or wrong token. |
| 403 | The request came from outside this computer, or carried an `Origin` or `Referer` header. |
| 404 | `close`, `input`: no open tab has that id. Also the IDE's reply to a browser-like request. |
| 405 | The method wasn't `POST`. |
| 409 | `open`: no project is open. |
| 415 | `Content-Type` wasn't `application/json`. |
| 503 | The IDE didn't respond within 10 seconds, usually because a dialog is open. Nothing happens later. |

The API accepts requests only from this computer, and only with the token. On macOS and Linux, only
your user account can read the registry folder. The `Origin`, `Referer` and `Content-Type` rules stop
web pages from opening sessions.

## Build from source

You need JDK 25. The Java runtime bundled with Android Studio works. Run every command from the
`jetbrains/` folder.

1. Build and test:

   ```sh
   ./gradlew test buildPlugin
   ```

   On Windows, run `.\gradlew.bat test buildPlugin`.

   The zip is in `build/distributions`.

2. Check the plugin against IntelliJ IDEA and Android Studio with the Plugin Verifier:

   ```sh
   ./gradlew verifyPlugin
   ```

   The verifier downloads each IDE that `gradle.properties` names, about 1.6 GB each.

By default, the build downloads IntelliJ IDEA at `platformVersion` from `gradle.properties` and
compiles against it. The first build downloads about 1.6 GB; later builds use Gradle's cache.

To build against an IDE you have installed, set `studioPath` in `~/.gradle/gradle.properties`, not in
the project's `gradle.properties`:

```properties
studioPath=C:/Program Files/Android/Android Studio
```

On macOS, the path is the app's `Contents` folder, for example
`studioPath=/Applications/Android Studio.app/Contents`. To download IntelliJ IDEA for one build while
`studioPath` is set, pass an empty value: `./gradlew test buildPlugin -PstudioPath=`.

### Local update repository

`./gradlew publishLocal` copies the zip and an `updatePlugins.xml` file into
`~/.ide-agent-tabs/repository`. Add that file's `file:///` URL once in **Settings > Plugins > ⚙ >
Manage Plugin Repositories**. After that, raise `pluginVersion` in `gradle.properties` and run
`publishLocal` again, and the IDE offers the update. Set `pluginRepositoryDir` to publish somewhere
else.

### Sandbox IDE

`./gradlew runIdeWithClaude` starts a separate IDE with this plugin and the Claude Code plugin. Set
`claudeCodePluginPath` in `~/.gradle/gradle.properties` to the Claude Code plugin's folder inside your
IDE's plugins folder, for example:

```properties
claudeCodePluginPath=C:/Users/<you>/AppData/Roaming/Google/AndroidStudio2026.2/plugins/claude-code-jetbrains-plugin
```

The sandbox uses `build/sandbox-home` in place of `~/.ide-agent-tabs`, so its registry file and
settings never mix with your real IDE's. The Java system property `ide.agent.tabs.home` sets this
folder.

## License

Proprietary. See [LICENSE](../LICENSE). The bundled third-party licenses are in
`claude-plugin/dist/THIRD_PARTY_NOTICES.txt`.
