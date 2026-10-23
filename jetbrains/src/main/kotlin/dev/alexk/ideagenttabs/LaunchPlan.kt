package dev.alexk.ideagenttabs

import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path

// Pure module: no IDE types, so launch planning tests without a running IDE.

const val ORI_COMMAND = "ori"
const val ORI_PREFIX = "ori:"
val ORI_PROFILES = setOf("claude", "codex", "grok", "hermes", "opencode", "pi", "prime-agent")
private val ORI_CMD_UNSAFE = Regex("[|\"%^&<>]")
private val SHIM_EXTENSIONS = listOf(".exe", ".cmd", ".bat")
private val WINDOWS_EXTENSIONS = listOf(".exe", ".cmd", ".bat", ".ps1")

private fun pathDirs(path: String): List<String> =
    path.split(File.pathSeparatorChar).map { it.trim().trim('"') }.filter { it.isNotEmpty() }

class LaunchContext(
    val prompt: String? = null,
    val args: List<String> = emptyList(),
    val env: Map<String, String> = emptyMap(),
    val model: String? = null,
    val via: LaunchVia? = null,
    val setting: LaunchVia = LaunchVia.DIRECT,
    val ori: DetectedOri? = null,
    val windows: Boolean = false,
    val searchPath: String = "",
    val python: List<String>? = null,
    val http: CodexHttp? = null,
    val copilot: CopilotHttp? = null,
    val claudeSettings: String? = null,
    val cwd: String? = null,
    val warn: (String) -> Unit = {},
)

private fun isCmdShim(command: String, searchPath: String): Boolean {
    val lower = command.lowercase()
    if (lower.endsWith(".cmd") || lower.endsWith(".bat")) return true
    if (lower.endsWith(".exe") || lower.endsWith(".com") || lower.endsWith(".ps1")) return false
    for (dir in pathDirs(searchPath)) {
        val hit = SHIM_EXTENSIONS.firstOrNull { ext ->
            runCatching { Path.of(dir, command + ext) }.getOrNull()?.let { Files.exists(it, LinkOption.NOFOLLOW_LINKS) } == true
        }
        if (hit != null) return hit != ".exe"
    }
    return true
}

fun oriLaunchable(name: String, ori: DetectedOri?): Boolean = ori != null && name in ori.agents && name in ORI_PROFILES

private fun oriRefusal(profile: AgentProfile, context: LaunchContext, oriArgs: List<String>): String? {
    val ori = context.ori ?: return "Ori is not installed"
    if (profile.name !in ORI_PROFILES) return "Ori does not support ${profile.name}"
    if (profile.name !in ori.agents) return "Ori does not list ${profile.name} as launchable"
    if (context.windows && isCmdShim(profile.command, context.searchPath) && oriArgs.any { ORI_CMD_UNSAFE.containsMatchIn(it) }) {
        return "Ori refuses an argument with | \" % ^ & < or > when the agent is a .cmd shim on Windows"
    }
    return null
}

private val GOOSE_RUN_ARGS = listOf("run", "-s")
private val GOOSE_EMPTY_ARGS = listOf("session")

private fun withoutPrompt(profile: AgentProfile, prompt: String?): AgentProfile =
    if (prompt == null && profile.command == "goose" && profile.args == GOOSE_RUN_ARGS) profile.copy(args = GOOSE_EMPTY_ARGS) else profile

private fun oriArgs(profile: AgentProfile, context: LaunchContext, callerArgs: List<String>): List<String> {
    val flag = listOfNotNull(profile.promptFlag?.takeIf { context.prompt != null })
    val model = context.model?.let { listOf("--model", it) }.orEmpty()
    return listOf(profile.name) + model + profile.args + callerArgs + flag
}

// Reads the route off the profile's own arguments, so it makes none of planLaunch's file reads for the Codex,
// Copilot and Claude arguments. Those arguments add no character that Ori refuses.
fun planVia(requested: AgentProfile, context: LaunchContext): LaunchVia {
    if ((context.via ?: context.setting) != LaunchVia.ORI) return LaunchVia.DIRECT
    val profile = withoutPrompt(requested, context.prompt)
    val refusal = oriRefusal(profile, context, oriArgs(profile, context, context.args) + listOfNotNull(context.prompt))
    return if (refusal == null) LaunchVia.ORI else LaunchVia.DIRECT
}

fun planLaunch(requested: AgentProfile, context: LaunchContext): AgentLaunch {
    val unplanned = withoutPrompt(requested, context.prompt)
    val codexArgs = withCodexHttp(unplanned.args, context.http) ?: withCodexPython(unplanned.args, context.python)
    val (copilotArgs, copilotEnv) = withCopilotHttp(unplanned.command, codexArgs, unplanned.env, context.copilot, context.cwd)
    val codex = unplanned.copy(args = copilotArgs, env = copilotEnv)
    val (ownArgs, callerArgs) = withClaudeSettings(codex.command, codex.args, context.args, context.claudeSettings, context.cwd, context.warn)
    val profile = codex.copy(args = ownArgs)
    if ((context.via ?: context.setting) == LaunchVia.ORI) {
        val args = oriArgs(profile, context, callerArgs)
        val refusal = oriRefusal(profile, context, args + listOfNotNull(context.prompt))
        if (refusal == null) {
            return AgentLaunch(profile.name, ORI_COMMAND, args, context.prompt, profile.env + context.env, LaunchVia.ORI)
        }
        if (context.via == LaunchVia.ORI) throw IllegalArgumentException("${profile.name} can't launch through Ori: $refusal")
    }
    if (context.model != null && profile.modelFlag == null) {
        throw IllegalArgumentException(
            "${profile.name} has no model option; open it without model, or set modelFlag for it in agents.json",
        )
    }
    return profile.launch(context.prompt, callerArgs, context.env, context.model)
}

fun isInstalled(command: String, path: String, isWindows: Boolean): Boolean {
    val names = if (isWindows) listOf(command) + WINDOWS_EXTENSIONS.map { command + it } else listOf(command)
    val direct = runCatching { Path.of(command) }.getOrNull()
    if (direct != null && direct.isAbsolute) {
        return names.any { Files.exists(Path.of(it), LinkOption.NOFOLLOW_LINKS) }
    }
    if (command.contains('/') || command.contains(File.separatorChar)) return false
    return names.any { findOnPath(path, it) != null }
}

// The Microsoft Store pwsh.exe under WindowsApps is an app execution alias that the JVM cannot follow, so
// PathEnvironmentVariableUtil.findInPath and a following Files.exists miss it and the tab starts Windows
// PowerShell 5.1. Checking the link itself finds it.
fun findOnPath(path: String, executable: String): String? =
    pathDirs(path)
        .firstNotNullOfOrNull { dir ->
            runCatching { Path.of(dir, executable) }.getOrNull()?.takeIf { Files.exists(it, LinkOption.NOFOLLOW_LINKS) }?.toString()
        }
