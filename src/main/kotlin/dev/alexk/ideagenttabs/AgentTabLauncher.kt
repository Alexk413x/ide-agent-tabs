package dev.alexk.ideagenttabs

import com.google.gson.JsonArray
import com.intellij.openapi.application.PathManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerKeys
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.SystemInfo
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.util.UUID

const val PLUGIN_ENV_PREFIX = "IDE_AGENT_TABS_"
const val PROMPT_ENV = "${PLUGIN_ENV_PREFIX}PROMPT"
const val TAB_ID_ENV = "${PLUGIN_ENV_PREFIX}ID"
const val AGENT_ENV = "${PLUGIN_ENV_PREFIX}AGENT"
const val COMMAND_ENV = "${PLUGIN_ENV_PREFIX}COMMAND"
const val ARGS_ENV = "${PLUGIN_ENV_PREFIX}ARGS"
const val ARG_COUNT_ENV = "${PLUGIN_ENV_PREFIX}ARGC"
const val ARG_ENV_PREFIX = "${PLUGIN_ENV_PREFIX}ARG_"
const val STARTUP_ENV = "JEDITERM_SOURCE"

fun isReservedEnv(name: String): Boolean =
    name.startsWith(PLUGIN_ENV_PREFIX, ignoreCase = true) || name.startsWith(STARTUP_ENV, ignoreCase = true)

enum class ShellKind { POWERSHELL, POSIX, FISH }

class Shell(val path: String, val kind: ShellKind, val flags: List<String> = emptyList())

fun shellKind(path: String): ShellKind? = when (File(path).name.lowercase().removeSuffix(".exe")) {
    "pwsh", "powershell" -> ShellKind.POWERSHELL
    "bash", "zsh" -> ShellKind.POSIX
    "fish" -> ShellKind.FISH
    else -> null
}

fun windowsShell(path: String): Shell =
    Shell(findOnPath(path, "pwsh.exe") ?: findOnPath(path, "powershell.exe") ?: "powershell.exe", ShellKind.POWERSHELL)

// The terminal only runs JEDITERM_SOURCE through its bash, zsh, fish and PowerShell integrations, so any
// other login shell falls back to bash. The terminal adds -l (macOS) and -i only to its own default shell,
// never to a given shell command, so they are added here; without -l, macOS skips ~/.zprofile and with it
// the Homebrew PATH.
fun unixShell(loginShell: String?, isMac: Boolean): Shell {
    val flags = if (isMac) listOf("-l", "-i") else listOf("-i")
    val bash = Shell("/bin/bash", ShellKind.POSIX, flags)
    if (loginShell == null) return bash
    return when (val kind = shellKind(loginShell)) {
        null -> bash
        ShellKind.POWERSHELL -> Shell(loginShell, kind)
        else -> Shell(loginShell, kind, flags)
    }
}

object AgentTabLauncher {

    @RequiresEdt
    fun open(
        project: Project,
        directory: String,
        profile: AgentProfile,
        prompt: String?,
        focus: Boolean,
        args: List<String> = emptyList(),
        env: Map<String, String> = emptyMap(),
    ): String {
        val id = UUID.randomUUID().toString()
        val shell = if (SystemInfo.isWindows) {
            windowsShell(System.getenv("PATH").orEmpty())
        } else {
            unixShell(System.getenv("SHELL"), SystemInfo.isMac)
        }
        val agent = profile.launch(prompt, args, env)
        val launch = when (shell.kind) {
            ShellKind.POWERSHELL -> powerShellLaunch(shell.path, agent, id)
            else -> sourcedLaunch(shell, launchScript(shell.kind, SCRIPT_DIR), agent, id)
        }
        val manager = TerminalToolWindowTabsManager.getInstance(project)
        val tab = manager.createTabBuilder()
            .workingDirectory(directory)
            .shellCommand(launch.command)
            .envVariables(launch.env)
            .tabName(profile.label)
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
        AgentTabRegistry.getInstance().add(AgentTabRegistry.Entry(id, profile.name, project, file, directory))
        return id
    }

    @RequiresEdt
    fun close(entry: AgentTabRegistry.Entry) {
        if (!entry.project.isDisposed) FileEditorManager.getInstance(entry.project).closeFile(entry.file)
    }

    private val SCRIPT_DIR: Path get() = PathManager.getSystemDir().resolve("ide-agent-tabs")
}

// The bash, zsh and fish integrations source JEDITERM_SOURCE as a file, so the script ships as a resource
// and is copied out. It is rewritten only when it differs, so a shell starting in another tab never reads
// a half-written file.
fun launchScript(kind: ShellKind, dir: Path): Path {
    val name = if (kind == ShellKind.FISH) "agent.fish" else "agent.sh"
    val content = ShellLaunch::class.java.getResourceAsStream("/launch/$name")!!.use { it.readAllBytes() }
    val target = dir.resolve(name)
    if (Files.isRegularFile(target) && Files.readAllBytes(target).contentEquals(content)) return target
    Files.createDirectories(dir)
    val temp = Files.createTempFile(dir, name, ".tmp")
    Files.write(temp, content)
    Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    return target
}

class ShellLaunch(val command: List<String>, val env: Map<String, String>)

private fun launchEnv(agent: AgentLaunch, tabId: String): MutableMap<String, String> =
    agent.env.toMutableMap().apply {
        this[TAB_ID_ENV] = tabId
        this[AGENT_ENV] = agent.agent
        this[COMMAND_ENV] = agent.command
    }

// The terminal appends its own "-NoExit -ExecutionPolicy Bypass -File powershell-integration.ps1" after
// the shell's arguments, so any "-Command" here would swallow them. The shell gets no arguments;
// the integration script runs JEDITERM_SOURCE through Invoke-Expression once its setup is done.
// The command, prompt and args travel in environment variables, never in that string, so nothing a caller
// sends is parsed as PowerShell.
fun powerShellLaunch(shell: String, agent: AgentLaunch, tabId: String): ShellLaunch {
    val env = launchEnv(agent, tabId)
    val setup = mutableListOf("\$c = \$env:$COMMAND_ENV; Remove-Item env:$COMMAND_ENV")
    var command = "& \$c"
    if (agent.args.isNotEmpty()) {
        env[ARGS_ENV] = JsonArray().apply { agent.args.forEach(::add) }.toString()
        setup += "\$a = @(\$env:$ARGS_ENV | ConvertFrom-Json); Remove-Item env:$ARGS_ENV"
        command += " @a"
    }
    if (agent.prompt != null) {
        env[PROMPT_ENV] = agent.prompt
        setup += "\$p = \$env:$PROMPT_ENV; Remove-Item env:$PROMPT_ENV"
        command += " \$p"
    }
    env[STARTUP_ENV] = (setup + command).joinToString("; ")
    return ShellLaunch(listOf(shell), env)
}

// bash, zsh and fish have no JSON parser, so unlike the PowerShell launch each arg travels in its own variable.
fun sourcedLaunch(shell: Shell, script: Path, agent: AgentLaunch, tabId: String): ShellLaunch {
    val env = launchEnv(agent, tabId)
    if (agent.args.isNotEmpty()) {
        env[ARG_COUNT_ENV] = agent.args.size.toString()
        agent.args.forEachIndexed { i, arg -> env["$ARG_ENV_PREFIX$i"] = arg }
    }
    if (agent.prompt != null) env[PROMPT_ENV] = agent.prompt
    env[STARTUP_ENV] = script.toString()
    return ShellLaunch(listOf(shell.path) + shell.flags, env)
}
