# Claude Studio Tabs

A plugin for Android Studio and other JetBrains IDEs that opens a new Claude Code session in an editor
tab.

- Click **New Claude Tab** in the main toolbar or the **Tools** menu to start a session in the project
  root.
- Other programs, such as another Claude session, can open, list and close tabs through a local HTTP
  endpoint. The IDE does not take focus.

## Requirements

- Android Studio 2026.2 or later, or another JetBrains IDE at build 262 or later.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed, with `claude` on your `PATH`.
- Terminal shell integration turned on. It is on by default: **Settings > Tools > Terminal > Shell
  integration**.

The plugin starts `claude` in these shells:

| OS | Shell |
|---|---|
| Windows | PowerShell 7 (`pwsh`), or Windows PowerShell when `pwsh` isn't installed |
| macOS, Linux | Your login shell (`$SHELL`) when it is bash, zsh, fish or `pwsh`; otherwise bash |

## Install

1. Build the plugin (see [Build from source](#build-from-source)), or get the
   `claude-studio-tabs-<version>.zip` file from someone who did.
2. In the IDE, open **Settings > Plugins**, click **⚙**, and choose **Install Plugin from Disk**.
3. Select the zip file, then restart the IDE.

## Open a tab from another program

When the IDE starts, the plugin writes the endpoint's addresses to `~/.claude-studio-tabs/endpoint.json`:

```json
{"url":"http://127.0.0.1:63342/claude-studio-tabs/open",
 "close":"http://127.0.0.1:63342/claude-studio-tabs/close",
 "list":"http://127.0.0.1:63342/claude-studio-tabs/list",
 "port":63342,"pid":12345}
```

Send each request as a `POST` with `Content-Type: application/json`. Every reply is JSON with
`"ok": true`, or `"ok": false` and an `"error"`.

macOS and Linux (uses `jq`):

```sh
url=$(jq -r .url ~/.claude-studio-tabs/endpoint.json)
curl -s "$url" -H 'Content-Type: application/json' \
  -d '{"path": "/path/to/repo", "prompt": "Your first message"}'
```

Windows PowerShell:

```powershell
$endpoint = Get-Content "$HOME\.claude-studio-tabs\endpoint.json" | ConvertFrom-Json
$body = @{ path = 'C:\path\to\repo'; prompt = 'Your first message' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $endpoint.url -ContentType 'application/json' -Body $body -UserAgent 'claude-studio-tabs'
```

In PowerShell, always pass `-UserAgent`. PowerShell's default User-Agent starts with `Mozilla/5.0`, and
the IDE refuses a browser-like `POST` before the plugin sees it. `curl` doesn't have this problem.

### Open

`POST` to `url`. The body takes these fields:

| Field | Required | Description |
|---|---|---|
| `path` | Yes | Absolute path to an existing folder. The session starts there. |
| `prompt` | No | The session's first message. Up to 30,000 characters. |
| `args` | No | Extra `claude` arguments, such as `["--plugin-dir", "/path/to/plugin"]`. Up to 64 strings. |
| `env` | No | Environment variables for the session, such as `{"MY_SETTING": "value"}`. Up to 64. Names that start with `CLAUDE_STUDIO_TABS_` or `JEDITERM_SOURCE` are refused. |

The reply holds the tab's `id`, the `project` window it opened in, and the `path`. The tab opens in the
open project that contains `path`, or in the last focused project window if none does.

### Close

`POST {"id": "<tab id>"}` to `close`. Closing the tab ends its session. You can close only tabs this
plugin opened. Each session can read its own id from the `CLAUDE_STUDIO_TABS_ID` environment variable,
so a session can close its own tab when it finishes.

### List

`POST {}` to `list`. The reply's `tabs` array holds the `id`, `project` and `path` of each open tab
the plugin opened.

### Status codes

| Status | Meaning |
|---|---|
| 200 | Done. |
| 400 | Bad body, relative path, missing folder, or missing `id`. |
| 403 | The request came from outside this computer, or carried an `Origin` or `Referer` header. |
| 404 | `close`: no open tab has that id. Also the IDE's reply to a browser-like request. |
| 405 | The method wasn't `POST`. |
| 409 | `open`: no project is open. |
| 415 | `Content-Type` wasn't `application/json`. |
| 503 | The IDE didn't respond within 10 seconds, usually because a dialog is open. Nothing opens later. |

The endpoint accepts requests only from this computer. The `Origin`, `Referer` and `Content-Type` rules
stop web pages from opening sessions.

## Build from source

You need JDK 25. The Java runtime bundled with Android Studio works.

1. Point `studioPath` at your IDE install. The value in `gradle.properties` is a Windows path, so on
   another machine set it in `~/.gradle/gradle.properties` instead, for example
   `studioPath=/Applications/Android Studio.app/Contents`.
2. Build and test:

   ```sh
   ./gradlew test buildPlugin
   ```

   On Windows, run `.\gradlew.bat test buildPlugin`.

The zip is in `build/distributions`.

### Local update repository

`./gradlew publishLocal` copies the zip and an `updatePlugins.xml` file into
`~/.claude-studio-tabs/repository`. Add that file's `file:///` URL once in **Settings > Plugins > ⚙ >
Manage Plugin Repositories**. After that, raise `pluginVersion` in `gradle.properties` and run
`publishLocal` again, and the IDE offers the update. Set `pluginRepositoryDir` to publish somewhere
else.

### Sandbox IDE

`./gradlew runIdeWithClaude` starts a separate IDE with this plugin and the Claude Code plugin from
`claudeCodePluginPath`. The sandbox writes its endpoint file to `build/sandbox-endpoint.json`, so it
never replaces your real IDE's file.

## License

[MIT](LICENSE)
