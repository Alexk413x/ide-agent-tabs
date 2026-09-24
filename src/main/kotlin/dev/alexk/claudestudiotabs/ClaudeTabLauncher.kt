package dev.alexk.claudestudiotabs

import com.intellij.execution.configurations.PathEnvironmentVariableUtil
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerKeys
import com.intellij.openapi.project.Project
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager
import com.intellij.util.concurrency.annotations.RequiresEdt

const val PROMPT_ENV = "CLAUDE_STUDIO_TABS_PROMPT"

object ClaudeTabLauncher {

    @RequiresEdt
    fun open(project: Project, directory: String, prompt: String?, focus: Boolean) {
        val manager = TerminalToolWindowTabsManager.getInstance(project)
        val builder = manager.createTabBuilder()
            .workingDirectory(directory)
            .shellCommand(claudeCommand(shell(), prompt != null))
            .tabName("Claude")
            .requestFocus(false)
        if (prompt != null) builder.envVariables(mapOf(PROMPT_ENV to prompt))
        val tab = builder.createTab()

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

    private fun shell(): String =
        PathEnvironmentVariableUtil.findInPath("pwsh.exe")?.path
            ?: PathEnvironmentVariableUtil.findInPath("powershell.exe")?.path
            ?: "powershell.exe"
}

fun claudeCommand(shell: String, withPrompt: Boolean, claude: String = "claude"): List<String> {
    val script = if (withPrompt) {
        "\$p = \$env:$PROMPT_ENV; Remove-Item env:$PROMPT_ENV; $claude \$p"
    } else {
        claude
    }
    return listOf(shell, "-NoLogo", "-NoExit", "-Command", script)
}
