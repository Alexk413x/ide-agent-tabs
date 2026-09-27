---
name: setup
description: Set up Agent Tabs on this machine - check prerequisites, install the IDE extensions (JetBrains, VS Code, Antigravity and other VS Code-based editors), pick a default agent and terminal, and optionally add OpenAI's Codex plugin. Use after installing the ide-agent-tabs plugin, or when the user asks to set up, repair or check Agent Tabs.
argument-hint: "[--check]"
---

Set up Agent Tabs, one step at a time. With `--check`, only report what's installed and what's
missing; change nothing.

Arguments: `$ARGUMENTS`

Ask before each change. Show the exact command you will run. At the end, give a short table of what's
done, what's skipped, and anything the user must do by hand, such as restarting an IDE.

## 1. Prerequisites

| Check | Command | If it fails |
|---|---|---|
| Node.js 20 or later (runs the MCP server) | `node --version` | Ask the user to install Node.js LTS. The MCP tools stay unavailable until then. |
| GitHub CLI, signed in (downloads private releases) | `gh auth status` | Ask the user to run `! gh auth login`. |
| Repository access | `gh release list -R Alexk413x/ide-agent-tabs --limit 5` | The user needs read access to the repository. |

## 2. Download the newest releases

For each part below, find the newest release tag (`jetbrains-v*`, `vscode-v*`) and download it into
`~/.ide-agent-tabs/downloads/<tag>/`:

```sh
gh release download <tag> -R Alexk413x/ide-agent-tabs -D ~/.ide-agent-tabs/downloads/<tag> --clobber
```

Check each file against the release's `SHA256SUMS` (`sha256sum -c` in Bash, or `Get-FileHash` in
PowerShell). Stop if a checksum doesn't match.

If there are no releases yet, ask whether the user has a local build: a JetBrains zip from
`jetbrains/build/distributions/` or a `.vsix` from `vscode/`. Use those files instead.

## 3. JetBrains IDEs

Find installed JetBrains IDEs, including Android Studio:

- Windows: `C:\Program Files\Android\Android Studio*`, `C:\Program Files\JetBrains\*`,
  `%LOCALAPPDATA%\Programs\*`, and the JetBrains Toolbox apps folder.
- macOS: `/Applications/*.app` and `~/Applications/*.app` that contain `Contents/Resources/product-info.json`.
- Linux: `~/.local/share/JetBrains/Toolbox/apps/*`, `/opt/*`, and `/snap/*`.

Read each IDE's `product-info.json` to get its name and build number. The plugin needs build 262.10315 or later (2026.2.2).

Then set up the local plugin repository:

1. Copy the zip to `~/.ide-agent-tabs/repository/`.
2. Write `~/.ide-agent-tabs/repository/updatePlugins.xml`:

   ```xml
   <plugins>
     <plugin id="dev.alexk.ide-agent-tabs" url="file:///<absolute path to the zip>" version="<version>">
       <idea-version since-build="262.10315"/>
       <name>Agent Tabs</name>
       <vendor>Alexk413x</vendor>
     </plugin>
   </plugins>
   ```

3. For each IDE, tell the user to add `file:///<home>/.ide-agent-tabs/repository/updatePlugins.xml` once
   in **Settings > Plugins > ⚙ > Manage Plugin Repositories**, then install **Agent Tabs** from
   **Marketplace** (it lists the custom repository's plugins), or use **Install Plugin from Disk** with
   the zip. Then restart the IDE.
4. If the old **Claude Studio Tabs** plugin (`dev.alexk.claude-studio-tabs`) is installed, tell the user
   to uninstall it first. Both plugins would add a toolbar button.

## 4. VS Code and VS Code-based editors

Find each editor's command-line tool: `code`, `code-insiders`, `cursor`, `windsurf`, and
`antigravity-ide`. Antigravity's tool is often not on `PATH`; on Windows it's at
`%LOCALAPPDATA%\Programs\Antigravity IDE\bin\antigravity-ide.cmd`.

For each one found:

```sh
<cli> --install-extension ~/.ide-agent-tabs/downloads/<tag>/ide-agent-tabs-<version>.vsix --force
```

Tell the user to reload open windows (**Developer: Reload Window**).

## 5. Agents and defaults

1. Call the MCP tool `list_agents` and show which agent CLIs are installed.
2. Ask which agent the **New Agent Tab** button should open by default, and write it to
   `~/.ide-agent-tabs/config.json` as `"defaultAgent"`. Keep any other keys in that file.
3. Ask which terminal app `open_tab` should use when no IDE fits: `windows-terminal` on Windows,
   `ghostty` on macOS or Linux. Write it as `"terminal"`.
4. To add a custom agent, such as one that runs a local LM Studio model, add a profile to
   `~/.ide-agent-tabs/agents.json`. The format is in the repository's `docs/design.md` under
   "Agent profiles".

## 6. Codex plugin (optional)

If `codex` is installed, offer OpenAI's Codex plugin for Claude Code. It gives `/codex:review` and
`/codex:rescue`, and the `delegate` skill uses it when present:

```sh
claude plugin marketplace add openai/codex-plugin-cc
claude plugin install codex@openai-codex
```

Then run `/reload-plugins`.

## 7. Check it works

1. Call `list_ides`. Every IDE with the extension installed and a window open should appear.
2. Offer to open a test tab with `open_tab` in the current folder, then close it with `close_tab`.
