package dev.alexk.claudestudiotabs

import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerKeys
import com.intellij.openapi.project.Project
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.util.UUID

const val PROMPT_ENV = "CLAUDE_STUDIO_TABS_PROMPT"
const val TAB_ID_ENV = "CLAUDE_STUDIO_TABS_ID"
const val STARTUP_ENV = "JEDITERM_SOURCE"

object ClaudeTabLauncher {

    @RequiresEdt
    fun open(project: Project, directory: String, prompt: String?, focus: Boolean): String {
        val id = UUID.randomUUID().toString()
        val launch = claudeLaunch(shell(), prompt, id)
        val manager = TerminalToolWindowTabsManager.getInstance(project)
        val tab = manager.createTabBuilder()
            .workingDirectory(directory)
            .shellCommand(launch.command)
            .envVariables(launch.env)
            .tabName("Claude")
            .requestFocus(false)
            .createTab()

        // Mirrors the terminal's own MoveTerminalSessionToEditorAction: the tab is detached from the
        // tool window, then reopened as an editor; CLOSING_TO_REOPEN keeps the session alive across the move.
        manager.detachTab(tab)
        val file = TerminalEditorFiles.of(tab)
        file.putUserData(FileEditorManagerKeys.CLOSING_TO_REOPEN, true)
        try {
            FileEditorManager.getInstance(project).openFile(file, focus)
        } finally {
            file.putUserData(FileEditorManagerKeys.CLOSING_TO_REOPEN, null)
        }
        ClaudeTabRegistry.getInstance().add(ClaudeTabRegistry.Entry(id, project, file, directory))
        return id
    }

    @RequiresEdt
    fun close(entry: ClaudeTabRegistry.Entry) {
        if (!entry.project.isDisposed) FileEditorManager.getInstance(entry.project).closeFile(entry.file)
    }

    private fun shell(): String {
        val path = System.getenv("PATH").orEmpty()
        return findOnPath(path, "pwsh.exe") ?: findOnPath(path, "powershell.exe") ?: "powershell.exe"
    }
}

// The Microsoft Store pwsh.exe under WindowsApps is an app execution alias that the JVM cannot follow, so
// PathEnvironmentVariableUtil.findInPath and a following Files.exists miss it and the tab starts Windows
// PowerShell 5.1. Checking the link itself finds it.
fun findOnPath(path: String, executable: String): String? =
    path.split(File.pathSeparatorChar)
        .map { it.trim().trim('"') }
        .filter { it.isNotEmpty() }
        .firstNotNullOfOrNull { dir ->
            runCatching { Path.of(dir, executable) }.getOrNull()?.takeIf { Files.exists(it, LinkOption.NOFOLLOW_LINKS) }?.toString()
        }

class ClaudeLaunch(val command: List<String>, val env: Map<String, String>)

// The terminal appends its own "-NoExit -ExecutionPolicy Bypass -File powershell-integration.ps1" after
// the shell's arguments, so any "-Command" here would swallow them. The shell gets no arguments;
// the integration script runs JEDITERM_SOURCE through Invoke-Expression once its setup is done.
fun claudeLaunch(shell: String, prompt: String?, tabId: String, claude: String = "claude"): ClaudeLaunch {
    val env = mutableMapOf(TAB_ID_ENV to tabId)
    if (prompt == null) {
        env[STARTUP_ENV] = claude
    } else {
        env[STARTUP_ENV] = "\$p = \$env:$PROMPT_ENV; Remove-Item env:$PROMPT_ENV; $claude \$p"
        env[PROMPT_ENV] = prompt
    }
    return ClaudeLaunch(listOf(shell), env)
}
