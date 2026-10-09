package dev.alexk.ideagenttabs

import com.google.gson.GsonBuilder
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.attribute.FileTime
import java.security.MessageDigest

// Pure module: no IDE or Netty types, so profiles and settings test without a running IDE.

const val DEFAULT_AGENT = "claude"
const val AGENTS_FILE = "agents.json"
const val CONFIG_FILE = "config.json"

private val PROFILE_NAME = Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
private val WINDOWS_EXTENSIONS = listOf(".exe", ".cmd", ".bat", ".ps1")

data class AgentProfile(
    val name: String,
    val label: String,
    val command: String,
    val args: List<String> = emptyList(),
    val promptFlag: String? = null,
    val env: Map<String, String> = emptyMap(),
    val icon: String? = null,
    val modelFlag: String? = null,
) {
    fun launch(
        prompt: String?,
        callerArgs: List<String> = emptyList(),
        callerEnv: Map<String, String> = emptyMap(),
        model: String? = null,
    ) = AgentLaunch(
        agent = name,
        command = command,
        args = args + listOfNotNull(modelFlag?.let { flag -> model?.let { listOf(flag, it) } }).flatten() + callerArgs +
            listOfNotNull(promptFlag?.takeIf { prompt != null }),
        prompt = prompt,
        env = env + callerEnv,
        via = LaunchVia.DIRECT,
    )
}

class AgentLaunch(
    val agent: String,
    val command: String,
    val args: List<String>,
    val prompt: String?,
    val env: Map<String, String>,
    val via: LaunchVia = LaunchVia.DIRECT,
)

const val ORI_COMMAND = "ori"
val ORI_PROFILES = setOf("claude", "codex", "grok", "hermes", "opencode", "pi", "prime-agent")
private val ORI_CMD_UNSAFE = Regex("[|\"%^&<>]")
private val SHIM_EXTENSIONS = listOf(".exe", ".cmd", ".bat")

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
    val claudeSettings: String? = null,
    val cwd: String? = null,
)

private fun isCmdShim(command: String, searchPath: String): Boolean {
    val lower = command.lowercase()
    if (lower.endsWith(".cmd") || lower.endsWith(".bat")) return true
    if (lower.endsWith(".exe") || lower.endsWith(".com") || lower.endsWith(".ps1")) return false
    for (raw in searchPath.split(File.pathSeparatorChar)) {
        val dir = raw.trim().trim('"')
        if (dir.isEmpty()) continue
        val hit = SHIM_EXTENSIONS.firstOrNull { ext ->
            runCatching { Path.of(dir, command + ext) }.getOrNull()?.let { Files.exists(it, LinkOption.NOFOLLOW_LINKS) } == true
        }
        if (hit != null) return hit != ".exe"
    }
    return true
}

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

fun planLaunch(requested: AgentProfile, context: LaunchContext): AgentLaunch {
    val unplanned = withoutPrompt(requested, context.prompt)
    val codex = unplanned.copy(args = withCodexPython(unplanned.args, context.python))
    val (ownArgs, callerArgs) = withClaudeSettings(codex.command, codex.args, context.args, context.claudeSettings, context.cwd)
    val profile = codex.copy(args = ownArgs)
    if ((context.via ?: context.setting) == LaunchVia.ORI) {
        val flag = listOfNotNull(profile.promptFlag?.takeIf { context.prompt != null })
        val model = context.model?.let { listOf("--model", it) }.orEmpty()
        val args = listOf(profile.name) + model + profile.args + callerArgs + flag
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

// Same strings as CODEX_TAB_ARGS in claude-plugin/mcp/src/ide_agent_tabs/profiles.py, which explains them; mcp/tests/test_codex_tab.py checks both.
val CODEX_TAB_ARGS = listOf(
    "--no-daemon",
    "-c",
    "mcp_servers.ide-agent-tabs={ command = 'python3', args = ['-I', '-S', '-c', '''import os,runpy;h=os.environ.get('IDE_AGENT_TABS_HOME') or os.path.join(os.path.expanduser('~'),'.ide-agent-tabs');runpy.run_path(os.path.join(h,'mcp','py','launch','mcp_server.py'),run_name='__main__')'''], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], tool_timeout_sec = 660 }",
    "-c",
    "hooks.UserPromptSubmit=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'UserPromptSubmit', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PostToolUse=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PostToolUse', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PermissionRequest=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PermissionRequest', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Stop=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Stop', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Interrupt=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Interrupt', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 3 }] }]",
    "-c",
    "hooks.state={ '/<session-flags>/config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, 'C:\\<session-flags>\\config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, '/<session-flags>/config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, 'C:\\<session-flags>\\config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, '/<session-flags>/config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, 'C:\\<session-flags>\\config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, '/<session-flags>/config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, 'C:\\<session-flags>\\config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, '/<session-flags>/config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' }, 'C:\\<session-flags>\\config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' } }",
)

private val CODEX_SERVER_ARG = CODEX_TAB_ARGS[2]
private const val CODEX_SERVER_HEAD = "mcp_servers.ide-agent-tabs={ command = 'python3', args = ["

// A Codex tab runs its server on the interpreter that python.json records, so no py.exe stays behind as its parent.
// The path goes into a TOML literal string and through cmd.exe, so it may not hold a quote, % or !, or end in \.
private val CODEX_PYTHON_SAFE = Regex("[^'\"%!\\u0000-\\u001f\\u007f]*[^'\"%!\\u0000-\\u001f\\u007f\\\\]")

fun withCodexPython(args: List<String>, python: List<String>?): List<String> {
    if (python.isNullOrEmpty()) return args
    val head = python.drop(1).joinToString("") { "'$it', " }
    val arg = "mcp_servers.ide-agent-tabs={ command = '${python[0]}', args = [$head${CODEX_SERVER_ARG.substring(CODEX_SERVER_HEAD.length)}"
    return args.map { if (it == CODEX_SERVER_ARG) arg else it }
}

fun recordedPython(home: Path, windows: Boolean): String? {
    val python = runCatching {
        JsonParser.parseString(Files.readString(home.resolve("mcp").resolve("python.json"))).asJsonObject.get("python")?.asString
    }.getOrNull()
    val absolute = python != null && (if (windows) Regex("([A-Za-z]:)?[\\\\/].*").matches(python) else python.startsWith("/"))
    if (python != null && absolute && CODEX_PYTHON_SAFE.matches(python) && runCatching { Files.isRegularFile(Path.of(python)) }.getOrDefault(false)) {
        return python
    }
    return null
}

fun codexPython(home: Path, windows: Boolean): List<String> =
    recordedPython(home, windows)?.let { listOf(it) } ?: if (windows) listOf("py", "-3") else listOf("python3")

// Same rules as claude_tab_settings and with_claude_settings in claude-plugin/mcp/src/ide_agent_tabs/profiles.py.
const val CLAUDE_TAB_SETTINGS_FILE = "claude-tab-settings.json"

// The settings path reaches cmd.exe when claude is a .cmd shim, so it may not hold a double quote or a cmd.exe metacharacter.
private val CMD_SAFE_PATH = Regex("[^\"%!^&|<>\\u0000-\\u001f\\u007f]+")
private val CLAUDE_COMMAND = Regex("(?:.*[\\\\/])?claude(?:\\.(?:exe|cmd|bat|ps1))?", RegexOption.IGNORE_CASE)

fun claudeTabSettings(home: Path, windows: Boolean): String? {
    val file = home.resolve("mcp").resolve(CLAUDE_TAB_SETTINGS_FILE)
    val path = file.toString()
    if (recordedPython(home, windows) == null || !CMD_SAFE_PATH.matches(path)) return null
    return if (runCatching { Files.isRegularFile(file) }.getOrDefault(false)) path else null
}

private fun withoutSettings(args: List<String>): Pair<List<String>, String?> {
    val kept = mutableListOf<String>()
    var value: String? = null
    var i = 0
    while (i < args.size) {
        val a = args[i]
        when {
            a == "--settings" && i + 1 < args.size -> value = args[++i]
            a.startsWith("--settings=") -> value = a.removePrefix("--settings=")
            else -> kept += a
        }
        i++
    }
    return kept to value
}

private fun readSettings(value: String, cwd: String?): JsonObject? = runCatching {
    val text = if (value.trim().startsWith("{")) {
        value
    } else {
        val absolute = value.startsWith("/") || Regex("([A-Za-z]:)?[\\\\/].*").matches(value)
        if (!absolute && cwd == null) return null
        Files.readString(if (absolute) Path.of(value) else Path.of(cwd!!).resolve(value))
    }
    JsonParser.parseString(text).takeIf { it.isJsonObject }?.asJsonObject
}.getOrNull()

fun mergedClaudeSettings(settings: String, value: String, cwd: String?): String? {
    val user = readSettings(value, cwd) ?: return null
    val ours = readSettings(settings, null)?.get("hooks")?.takeIf { it.isJsonObject }?.asJsonObject ?: return null
    val hooks = user.get("hooks") ?: JsonObject()
    if (!hooks.isJsonObject) return null
    val merged = hooks.asJsonObject.deepCopy()
    for ((event, groups) in ours.entrySet()) {
        val existing = merged.get(event) ?: JsonArray()
        if (!existing.isJsonArray || !groups.isJsonArray) return null
        merged.add(event, existing.asJsonArray.deepCopy().apply { addAll(groups.asJsonArray) })
    }
    val result = user.deepCopy().apply { add("hooks", merged) }
    val text = GsonBuilder().setPrettyPrinting().disableHtmlEscaping().create().toJson(result) + "\n"
    val digest = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }.take(16)
    val file = Path.of(settings).resolveSibling("${CLAUDE_TAB_SETTINGS_FILE.removeSuffix(".json")}-$digest.json")
    return runCatching {
        if (!Files.isRegularFile(file) || Files.readString(file) != text) writeAtomically(file, text)
        file.toString()
    }.getOrNull()
}

// claude reads only the last --settings, so a tab whose profile or request passes its own gets one file that
// merges those settings with the tab's hooks, and keeps the user's flag only when that merge is impossible.
fun withClaudeSettings(
    command: String,
    args: List<String>,
    callerArgs: List<String>,
    settings: String?,
    cwd: String? = null,
): Pair<List<String>, List<String>> {
    if (settings == null || !CLAUDE_COMMAND.matches(command)) return args to callerArgs
    val (own, ownValue) = withoutSettings(args)
    val (caller, callerValue) = withoutSettings(callerArgs)
    val value = callerValue ?: ownValue ?: return (args + listOf("--settings", settings)) to callerArgs
    val merged = mergedClaudeSettings(settings, value, cwd) ?: return args to callerArgs
    return (own + listOf("--settings", merged)) to caller
}

val BUILTIN_PROFILES = listOf(
    AgentProfile("claude", "Claude Code", "claude", modelFlag = "--model"),
    AgentProfile("codex", "Codex", "codex", CODEX_TAB_ARGS, modelFlag = "-m"),
    AgentProfile("agy", "Antigravity CLI", "agy", promptFlag = "-i", modelFlag = "--model"),
    AgentProfile("copilot", "Copilot CLI", "copilot", promptFlag = "-i", modelFlag = "--model"),
    AgentProfile("gemini", "Gemini CLI", "gemini", promptFlag = "-i", modelFlag = "-m"),
    AgentProfile("grok", "Grok Build", "grok", modelFlag = "-m"),
    AgentProfile("pi", "Pi", "pi", modelFlag = "--model"),
    AgentProfile("hermes", "Hermes", "hermes", listOf("chat"), promptFlag = "-q", modelFlag = "-m"),
    AgentProfile("opencode", "OpenCode", "opencode", promptFlag = "--prompt", modelFlag = "-m"),
    AgentProfile("qwen", "Qwen Code", "qwen", promptFlag = "-i", modelFlag = "-m"),
    AgentProfile("goose", "Goose", "goose", listOf("run", "-s"), promptFlag = "-t", modelFlag = "--model"),
    // Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
    AgentProfile("codex-local", "Codex (local)", "codex", CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "ollama"), modelFlag = "-m"),
)

fun parseProfiles(text: String): List<AgentProfile> {
    val root = parseJsonObject(text, AGENTS_FILE)
    return root.entrySet().map { (name, value) ->
        if (!PROFILE_NAME.matches(name)) throw IllegalArgumentException("profile name '$name' must be letters, digits, '.', '_' or '-'")
        if (!value.isJsonObject) throw IllegalArgumentException("profile $name must be an object")
        val obj = value.asJsonObject
        val command = obj.optString("$name.command", "command")
        if (command.isNullOrBlank() || '\u0000' in command) throw IllegalArgumentException("profile $name needs a command")
        val args = obj.optStringList("$name.args", "args")
        if (args.size > MAX_ENTRIES) throw IllegalArgumentException("$name.args exceeds $MAX_ENTRIES entries")
        if (args.any { '\u0000' in it }) throw IllegalArgumentException("$name.args holds a NUL")
        val promptFlag = obj.optString("$name.promptFlag", "promptFlag")
        if (promptFlag != null && (promptFlag.isBlank() || '\u0000' in promptFlag)) {
            throw IllegalArgumentException("$name.promptFlag must not be blank")
        }
        val modelFlag = obj.optString("$name.modelFlag", "modelFlag")
        if (modelFlag != null && (modelFlag.isBlank() || '\u0000' in modelFlag)) {
            throw IllegalArgumentException("$name.modelFlag must not be blank")
        }
        val env = obj.optStringMap("$name.env", "env")
        checkEnv(env, "$name.env")
        AgentProfile(
            name = name,
            label = obj.optString("$name.label", "label")?.takeIf { it.isNotBlank() } ?: name,
            command = command,
            args = args,
            promptFlag = promptFlag,
            env = env,
            icon = obj.optString("$name.icon", "icon")?.takeIf { it.isNotBlank() },
            modelFlag = modelFlag,
        )
    }
}

fun mergeProfiles(builtins: List<AgentProfile>, custom: List<AgentProfile>): List<AgentProfile> {
    val byName = custom.associateBy { it.name }
    return builtins.map { byName[it.name] ?: it } + custom.filter { c -> builtins.none { it.name == c.name } }
}

fun checkEnv(env: Map<String, String>, field: String) {
    if (env.size > MAX_ENTRIES) throw IllegalArgumentException("$field exceeds $MAX_ENTRIES entries")
    for ((name, value) in env) {
        if (name.isBlank() || name.any { it == '=' || it.isWhitespace() || it == '\u0000' }) {
            throw IllegalArgumentException("$field name is not a valid variable name: '$name'")
        }
        if (isReservedEnv(name)) throw IllegalArgumentException("$field name $name is reserved by the plugin")
        if (value.length > MAX_PROMPT_CHARS || '\u0000' in value) {
            throw IllegalArgumentException("$field $name is longer than $MAX_PROMPT_CHARS characters or holds a NUL")
        }
    }
}

fun readDefaultAgent(text: String): String? {
    val value = parseJsonObject(text, CONFIG_FILE).get("defaultAgent") ?: return null
    return if (value.isJsonPrimitive && value.asJsonPrimitive.isString) value.asString else null
}

fun withDefaultAgent(existing: String?, name: String): String {
    val root = if (existing.isNullOrBlank()) JsonObject() else parseJsonObject(existing, CONFIG_FILE)
    root.addProperty("defaultAgent", name)
    return GsonBuilder().setPrettyPrinting().create().toJson(root) + "\n"
}

internal fun parseJsonObject(text: String, file: String): JsonObject {
    val json = try {
        JsonParser.parseString(text)
    } catch (e: Exception) {
        throw IllegalArgumentException("$file is not JSON: ${e.message}")
    }
    if (!json.isJsonObject) throw IllegalArgumentException("$file must hold a JSON object")
    return json.asJsonObject
}

private fun JsonObject.optString(field: String, key: String): String? {
    val value: JsonElement = get(key) ?: return null
    if (value.isJsonNull) return null
    if (!value.isJsonPrimitive || !value.asJsonPrimitive.isString) throw IllegalArgumentException("$field must be a string")
    return value.asString
}

private fun JsonObject.optStringList(field: String, key: String): List<String> {
    val value = get(key) ?: return emptyList()
    if (value.isJsonNull) return emptyList()
    if (!value.isJsonArray) throw IllegalArgumentException("$field must be an array of strings")
    return value.asJsonArray.map {
        if (!it.isJsonPrimitive || !it.asJsonPrimitive.isString) throw IllegalArgumentException("$field must be an array of strings")
        it.asString
    }
}

private fun JsonObject.optStringMap(field: String, key: String): Map<String, String> {
    val value = get(key) ?: return emptyMap()
    if (value.isJsonNull) return emptyMap()
    if (!value.isJsonObject) throw IllegalArgumentException("$field must be an object of strings")
    return value.asJsonObject.entrySet().associate { (k, v) ->
        if (!v.isJsonPrimitive || !v.asJsonPrimitive.isString) throw IllegalArgumentException("$field.$k must be a string")
        k to v.asString
    }
}

class AgentSettings(private val home: Path, private val warn: (String) -> Unit) {

    private class Cached<T>(val stamp: Pair<FileTime, Long>?, val value: T)

    @Volatile
    private var profiles: Cached<List<AgentProfile>>? = null

    @Volatile
    private var defaultAgent: Cached<String?>? = null

    private val agentsFile get() = home.resolve(AGENTS_FILE)
    private val configFile get() = home.resolve(CONFIG_FILE)

    fun profiles(): List<AgentProfile> {
        val stamp = stamp(agentsFile)
        profiles?.takeIf { it.stamp == stamp }?.let { return it.value }
        val value = if (stamp == null) {
            BUILTIN_PROFILES
        } else {
            try {
                mergeProfiles(BUILTIN_PROFILES, parseProfiles(Files.readString(agentsFile)))
            } catch (e: Exception) {
                warn("Ignoring $agentsFile and using the built-in agent profiles: ${e.message}")
                BUILTIN_PROFILES
            }
        }
        profiles = Cached(stamp, value)
        return value
    }

    fun profile(name: String): AgentProfile? = profiles().firstOrNull { it.name == name }

    fun defaultProfile(): AgentProfile {
        val all = profiles()
        return all.firstOrNull { it.name == configuredDefault() } ?: all.first { it.name == DEFAULT_AGENT }
    }

    fun setDefaultAgent(name: String): Boolean = try {
        val existing = if (Files.isRegularFile(configFile)) Files.readString(configFile) else null
        writeAtomically(configFile, withDefaultAgent(existing, name))
        true
    } catch (e: Exception) {
        warn("Could not save the default agent to $configFile: ${e.message}")
        false
    }

    fun shared(): SharedSettings {
        if (!Files.isRegularFile(configFile)) return SharedSettings()
        return try {
            readSharedSettings(Files.readString(configFile))
        } catch (e: Exception) {
            warn("Ignoring $configFile: ${e.message}")
            SharedSettings()
        }
    }

    fun setTabRouting(value: TabRouting): Boolean = saveShared("tabRouting", value.value)

    fun setTerminal(value: String): Boolean = saveShared("terminal", value)

    fun setShell(value: String): Boolean = saveShared("shell", value)

    fun setTerminalWindow(value: TerminalWindow): Boolean = saveShared("terminalWindow", value.value)

    fun setLaunchVia(value: LaunchVia): Boolean = saveShared("launchVia", value.value)

    fun setFocusNewTabs(value: FocusNewTabs): Boolean = saveShared("focusNewTabs", value.value)

    fun setClaudeMod(on: Boolean): Boolean = saveShared("claudeMod", if (on) AUTO else CLAUDE_MOD_OFF)

    fun setCloseAfterHandoff(value: Boolean): Boolean = saveFlag("closeAfterHandoff", value, default = true)

    fun setAllowResume(value: Boolean): Boolean = saveFlag("allowResume", value, default = true)

    private fun saveFlag(key: String, value: Boolean, default: Boolean): Boolean = try {
        val existing = if (Files.isRegularFile(configFile)) Files.readString(configFile) else null
        writeAtomically(configFile, withSharedFlag(existing, key, value, default))
        true
    } catch (e: Exception) {
        warn("Could not save $key to $configFile: ${e.message}")
        false
    }

    fun detected(): Detected = try {
        parseDetected(Files.readString(home.resolve(DETECTED_FILE)))
    } catch (e: Exception) {
        Detected()
    }

    private fun saveShared(key: String, value: String): Boolean = try {
        val existing = if (Files.isRegularFile(configFile)) Files.readString(configFile) else null
        writeAtomically(configFile, withSharedValue(existing, key, value))
        true
    } catch (e: Exception) {
        warn("Could not save $key to $configFile: ${e.message}")
        false
    }

    private fun configuredDefault(): String? {
        val stamp = stamp(configFile)
        defaultAgent?.takeIf { it.stamp == stamp }?.let { return it.value }
        val value = if (stamp == null) {
            null
        } else {
            try {
                readDefaultAgent(Files.readString(configFile))
            } catch (e: Exception) {
                warn("Ignoring $configFile: ${e.message}")
                null
            }
        }
        defaultAgent = Cached(stamp, value)
        return value
    }

    private fun stamp(file: Path): Pair<FileTime, Long>? =
        runCatching { Files.getLastModifiedTime(file) to Files.size(file) }.getOrNull()
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
    path.split(File.pathSeparatorChar)
        .map { it.trim().trim('"') }
        .filter { it.isNotEmpty() }
        .firstNotNullOfOrNull { dir ->
            runCatching { Path.of(dir, executable) }.getOrNull()?.takeIf { Files.exists(it, LinkOption.NOFOLLOW_LINKS) }?.toString()
        }
