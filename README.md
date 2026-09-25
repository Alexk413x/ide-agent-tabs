# Claude Studio Tabs

An Android Studio plugin that opens a new Claude Code session in an editor tab. It never reuses a tab
and never types into one.

- **New Claude Tab** (main toolbar and Tools menu) opens a session in the project root.
- **A loopback HTTP endpoint** lets another process, such as a Claude session, open a session in any folder
  and hand it a first message. The IDE does not take focus.

Targets Android Studio build 262 (2026.2). Windows only: the session runs `claude` inside `pwsh`
(or Windows PowerShell when `pwsh` is not on `PATH`).

## Build and install

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio1\jbr"
.\gradlew.bat test buildPlugin
```

Install `build\distributions\claude-studio-tabs-<version>.zip` with **Settings > Plugins > ⚙ > Install
Plugin from Disk**, then restart the IDE. `studioPath` in `gradle.properties` names the Android Studio
install the plugin compiles against. A new Android Studio version is a change to `studioPath` and
`untilBuild`, then a rebuild.

## Updates

The plugin updates from a private plugin repository on this machine, `~/.claude-studio-tabs/repository`.

One-time setup: in **Settings > Plugins > ⚙ > Manage Plugin Repositories**, add
`file:///C:/Users/Alexk/.claude-studio-tabs/repository/updatePlugins.xml`.

To release a build, raise `pluginVersion` in `gradle.properties`, then run:

```powershell
.\gradlew.bat publishLocal
```

`publishLocal` builds the zip, copies it into the repository and writes `updatePlugins.xml`. The IDE
offers the update the next time it checks, or at once from **Settings > Plugins > Installed >
Check for Updates**. Installing an update needs an IDE restart. Set `pluginRepositoryDir` in
`gradle.properties` to publish somewhere else.

## Endpoint

On startup the plugin writes `~/.claude-studio-tabs/endpoint.json` (the sandbox from `runIdeWithClaude`
writes `build/sandbox-endpoint.json` instead, so it never replaces the real IDE's file):

```json
{"url":"http://127.0.0.1:63342/claude-studio-tabs/open",
 "close":"http://127.0.0.1:63342/claude-studio-tabs/close",
 "list":"http://127.0.0.1:63342/claude-studio-tabs/list",
 "port":63342,"pid":12345}
```

Every route takes a `POST` with `Content-Type: application/json` and answers JSON with `"ok": true` or
`"ok": false` and an `"error"`.

Set a User-Agent that does not start with `Mozilla/5.0`. The IDE's built-in server treats such a `POST`
without an `Origin` header as a browser write and answers 404 before the plugin sees it.
`Invoke-WebRequest` and `Invoke-RestMethod` send a `Mozilla/5.0` User-Agent by default; `curl` does not.

### Open a tab

```powershell
$endpoint = Get-Content "$HOME\.claude-studio-tabs\endpoint.json" | ConvertFrom-Json
$body = @{ path = 'C:\path\to\repo'; prompt = 'Your first message' } | ConvertTo-Json
$tab = Invoke-RestMethod -Method Post -Uri $endpoint.url -ContentType 'application/json' -Body $body -UserAgent 'claude-studio-tabs'
$tab.id
```

- `path` (required): an absolute path to an existing folder. The session starts there.
- `prompt` (optional, up to 30,000 characters): passed as `claude "<prompt>"`, so it becomes the
  session's first message.
- `args` (optional, an array of up to 64 strings): extra `claude` arguments, placed before the prompt,
  for example `["--plugin-dir", "C:\\path\\to\\plugin"]`. Each string reaches `claude` as one argument.
- `env` (optional, an object of up to 64 string values): environment variables set for the tab's shell
  and the session. Names that the plugin sets itself (`CLAUDE_STUDIO_TABS_*`, `JEDITERM_SOURCE`) are
  refused.

```powershell
$body = @{
    path   = 'C:\path\to\repo'
    prompt = 'Your first message'
    args   = @('--plugin-dir', 'C:\path\to\plugin')
    env    = @{ MY_SETTING = 'value' }
} | ConvertTo-Json
```

The reply carries the tab's `id`, the `project` window it opened in, and the `path`. The tab opens in
the open project that contains `path`. If no open project contains it, the tab opens in the last
focused project window. The button's tabs get an id too.

### Close a tab

```powershell
$body = @{ id = $tab.id } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $endpoint.close -ContentType 'application/json' -Body $body -UserAgent 'claude-studio-tabs'
```

Closing the editor tab ends its terminal session, including a running `claude`. Only tabs this plugin
opened can be closed; any other id answers 404. Each session holds its own id in the
`CLAUDE_STUDIO_TABS_ID` environment variable, so a session can close its own tab when its work is done.

### List tabs

```powershell
Invoke-RestMethod -Method Post -Uri $endpoint.list -ContentType 'application/json' -Body '{}' -UserAgent 'claude-studio-tabs'
```

The reply's `tabs` array holds `id`, `project` and `path` for every open tab the plugin opened.

### Status codes

| Status | Meaning |
|---|---|
| 200 | Done. |
| 400 | Bad body, relative path, missing folder, or missing `id`. |
| 403 | The request came from a non-loopback address or carried an `Origin` or `Referer` header. |
| 404 | `close`: no open tab with that id. Also the built-in server's answer to a browser-like request. |
| 405 | The method was not `POST`. |
| 409 | `open`: no project is open. |
| 415 | `Content-Type` was not `application/json`. |
| 503 | The IDE did not act within 10 seconds, usually because a modal dialog is open. Nothing happens later. |

The `Origin`, `Referer` and `Content-Type` rules keep web pages from opening sessions: a browser cannot
send a cross-origin JSON `POST` without a preflight, and it always sends `Origin` on the request.
