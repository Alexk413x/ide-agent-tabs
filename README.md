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

## Endpoint

On startup the plugin writes `~/.claude-studio-tabs/endpoint.json`:

```json
{"url":"http://127.0.0.1:63342/claude-studio-tabs/open","port":63342,"pid":12345}
```

Send one request:

```powershell
$endpoint = Get-Content "$HOME\.claude-studio-tabs\endpoint.json" | ConvertFrom-Json
$body = @{ path = 'C:\path\to\repo'; prompt = 'Your first message' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $endpoint.url -ContentType 'application/json' -Body $body -UserAgent 'claude-studio-tabs'
```

Set a User-Agent that does not start with `Mozilla/5.0`. The IDE's built-in server treats such a `POST`
without an `Origin` header as a browser write and answers 404 before the plugin sees it.
`Invoke-WebRequest` and `Invoke-RestMethod` send a `Mozilla/5.0` User-Agent by default; `curl` does not.

- `path` (required): an absolute path to an existing folder. The session starts there.
- `prompt` (optional, up to 30,000 characters): passed as `claude "<prompt>"`, so it becomes the
  session's first message.

The tab opens in the open project that contains `path`. If no open project contains it, the tab opens
in the last focused project window.

| Status | Meaning |
|---|---|
| 200 | Tab opened. The body names the project. |
| 400 | Bad body, relative path, or missing folder. |
| 403 | The request came from a non-loopback address or carried an `Origin` or `Referer` header. |
| 405 | The method was not `POST`. |
| 409 | No project is open. |
| 415 | `Content-Type` was not `application/json`. |

The `Origin`, `Referer` and `Content-Type` rules keep web pages from opening sessions: a browser cannot
send a cross-origin JSON `POST` without a preflight, and it always sends `Origin` on the request.
