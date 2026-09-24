package dev.alexk.claudestudiotabs

import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerKeys
import com.intellij.openapi.project.Project
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.io.File
import java.nio.file.Files
import java.nio.file.Path

const val PROMPT_ENV = "CLAUDE_STUDIO_TABS_PROMPT"
const val STARTUP_ENV = "JEDITERM_SOURCE"

object ClaudeTabLauncher {

    @RequiresEdt
    fun open(project: Project, directory: String, prompt: String?, focus: Boolean) {
        val launch = claudeLaunch(shell(), prompt)
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
    }

    private fun shell(): String {
        val path = System.getenv("PATH").orEmpty()
        return findOnPath(path, "pwsh.exe") ?: findOnPath(path, "powershell.exe") ?: "powershell.exe"
    }
}

// Not PathEnvironmentVariableUtil.findInPath: it misses the Microsoft Store pwsh.exe under WindowsApps,
// and the tab then starts Windows PowerShell 5.1 instead.
fun findOnPath(path: String, executable: String): String? =
    path.split(File.pathSeparatorChar)
        .map { it.trim().trim('"') }
        .filter { it.isNotEmpty() }
        .firstNotNullOfOrNull { dir ->
            runCatching { Path.of(dir, executable) }.getOrNull()?.takeIf { Files.exists(it) }?.toString()
        }

class ClaudeLaunch(val command: List<String>, val env: Map<String, String>)

// The terminal appends its own "-NoExit -ExecutionPolicy Bypass -File powershell-integration.ps1" after
// the shell's arguments, so any "-Command" here would swallow them. The shell gets no arguments;
// the integration script runs JEDITERM_SOURCE through Invoke-Expression once its setup is done.
fun claudeLaunch(shell: String, prompt: String?, claude: String = "claude"): ClaudeLaunch {
    if (prompt == null) return ClaudeLaunch(listOf(shell), mapOf(STARTUP_ENV to claude))
    val script = "\$p = \$env:$PROMPT_ENV; Remove-Item env:$PROMPT_ENV; $claude \$p"
    return ClaudeLaunch(listOf(shell), mapOf(STARTUP_ENV to script, PROMPT_ENV to prompt))
}
