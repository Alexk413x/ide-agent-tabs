package dev.alexk.ideagenttabs

import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.attribute.FileTime

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
) {
    fun launch(prompt: String?, callerArgs: List<String> = emptyList(), callerEnv: Map<String, String> = emptyMap()) =
        AgentLaunch(
            agent = name,
            command = command,
            args = args + callerArgs + listOfNotNull(promptFlag?.takeIf { prompt != null }),
            prompt = prompt,
            env = env + callerEnv,
        )
}

class AgentLaunch(
    val agent: String,
    val command: String,
    val args: List<String>,
    val prompt: String?,
    val env: Map<String, String>,
)

val BUILTIN_PROFILES = listOf(
    AgentProfile("claude", "Claude Code", "claude"),
    AgentProfile("codex", "Codex", "codex"),
    AgentProfile("gemini", "Gemini CLI", "gemini", promptFlag = "-i"),
    AgentProfile("copilot", "Copilot CLI", "copilot", promptFlag = "-i"),
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

private fun parseJsonObject(text: String, file: String): JsonObject {
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
