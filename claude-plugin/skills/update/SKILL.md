---
name: update
description: Update IDE Agent Tabs everywhere on this machine - the Claude Code plugin, the JetBrains plugin and the VS Code-family extensions - from the newest GitHub releases. Use when the user asks to update, upgrade or check the version of IDE Agent Tabs.
argument-hint: "[--check]"
---

Update every installed part of IDE Agent Tabs to its newest release. With `--check`, only report
installed and available versions; change nothing.

Arguments: `$ARGUMENTS`

## 1. Find what's installed and what's available

| Part | Installed version | Newest version |
|---|---|---|
| Claude Code plugin | `claude plugin list` | `claude plugin marketplace update ide-agent-tabs`, then `claude plugin list` |
| JetBrains plugin | The `version` in `~/.ide-agent-tabs/repository/updatePlugins.xml`, or the `version` from a running IDE's `info` (MCP `list_ides`) | Newest `jetbrains-v*` tag from `gh release list -R Alexk413x/ide-agent-tabs` |
| VS Code-family extension | `<cli> --list-extensions --show-versions`, look for `alexk413x.ide-agent-tabs` | Newest `vscode-v*` tag |

Show the table. Stop here with `--check`, or when everything is current.

## 2. Download and check

For each part with a newer release, download it and check its checksum, as in the setup skill:

```sh
gh release download <tag> -R Alexk413x/ide-agent-tabs -D ~/.ide-agent-tabs/downloads/<tag> --clobber
```

Stop if a file doesn't match `SHA256SUMS`.

## 3. Install

- **Claude Code plugin:** `claude plugin update ide-agent-tabs@ide-agent-tabs`, then tell the user to run
  `/reload-plugins`.
- **JetBrains plugin:** copy the new zip into `~/.ide-agent-tabs/repository/` and rewrite
  `updatePlugins.xml` with the new version and the new zip's `file:///` URL. Delete older zips in that
  folder. Tell the user that each JetBrains IDE offers the update at its next check, or at once from
  **Settings > Plugins > Installed > Check for Updates**, and that it needs a restart.
- **VS Code-family editors:** for each editor CLI found (see the setup skill), run
  `<cli> --install-extension <vsix> --force`, then tell the user to reload open windows.

## 4. Report

List each part with its old and new version, and each restart or reload the user must do.
