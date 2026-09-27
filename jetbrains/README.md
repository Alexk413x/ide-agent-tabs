# IDE Agent Tabs for JetBrains IDEs

A plugin for Android Studio and other JetBrains IDEs that opens AI coding-agent sessions, such as Claude
Code, Codex, Gemini CLI and Copilot CLI, in editor tabs.

- Click **New Agent Tab** in the main toolbar or the **Tools** menu to start the default agent in the
  project root.
- Right-click **New Agent Tab** in the main toolbar, or open **Tools > New Agent Tab With**, to choose
  another installed agent. The one you choose becomes the default.
- Other programs, such as another agent session, can open, list and close tabs through a local HTTP
  API. The IDE does not take focus.

## Requirements

- Android Studio 2026.2 or later, or another JetBrains IDE at build 262 or later.
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

1. Download `ide-agent-tabs-<version>.zip` from a `jetbrains-v<version>` release on the repository's
   GitHub Releases page, or build it (see [Build from source](#build-from-source)).
2. In the IDE, open **Settings > Plugins**, click **⚙**, and choose **Install Plugin from Disk**.
3. Select the zip file, then restart the IDE.

## Agents

The plugin knows these agents:

| Name | Label | Command | First prompt |
|---|---|---|---|
| `claude` | Claude Code | `claude` | positional |
| `codex` | Codex | `codex` | positional |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` |

The right-click menu lists only agents whose command is on the IDE's `PATH`. On Windows, the plugin
also looks for `.exe`, `.cmd`, `.bat` and `.ps1` files.

To add an agent or change a built-in one, create `~/.ide-agent-tabs/agents.json`. A profile with the
same name as a built-in one replaces it.

```json
{
  "opencode-local": {
    "label": "OpenCode (LM Studio)",
    "command": "opencode",
    "args": ["--model", "lmstudio/qwen3-coder"],
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
| `env` | No | Environment variables for the session. The caller's `env` wins on a clash. |
| `icon` | No | An SVG file for menus, as an absolute path or relative to `~/.ide-agent-tabs`. |

If `agents.json` isn't valid, the plugin logs a warning to `idea.log` and uses only the built-in
agents.

The default agent is in `~/.ide-agent-tabs/config.json`. Choosing an agent from a menu writes it:

```json
{ "defaultAgent": "claude" }
```

## Open a tab from another program

When the IDE starts, the plugin writes a registry file to
`~/.ide-agent-tabs/endpoints/jetbrains-<pid>.json`, and deletes it when the IDE closes:

```json
{"protocol":1,"ide":"jetbrains","product":"Android Studio","version":"2026.2.2","pid":12345,
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

The reply holds the tab's `id`, the `agent`, the `project` window it opened in, and the `path`. The tab
opens in the open project that contains `path`, or in the last focused project window if none does.

### Close

`POST {"id": "<tab id>"}` to `close`. Closing the tab ends its session. You can close only tabs this
plugin opened. Each session can read its own id from the `IDE_AGENT_TABS_ID` environment variable,
so a session can close its own tab when it finishes. `IDE_AGENT_TABS_AGENT` holds the agent name.

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
| 400 | Bad body, relative path, missing folder, missing `id`, or unknown `agent`. |
| 401 | Missing or wrong token. |
| 403 | The request came from outside this computer, or carried an `Origin` or `Referer` header. |
| 404 | `close`: no open tab has that id. Also the IDE's reply to a browser-like request. |
| 405 | The method wasn't `POST`. |
| 409 | `open`: no project is open. |
| 415 | `Content-Type` wasn't `application/json`. |
| 503 | The IDE didn't respond within 10 seconds, usually because a dialog is open. Nothing opens later. |

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

[MIT](../LICENSE)
