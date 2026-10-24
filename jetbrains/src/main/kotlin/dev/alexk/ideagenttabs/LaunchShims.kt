package dev.alexk.ideagenttabs

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import java.net.HttpURLConnection
import java.net.Proxy
import java.net.URI
import java.nio.file.Files
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.security.MessageDigest

// Pure module: no IDE types, so the Codex, Copilot and Claude arguments test without a running IDE.

private val WINDOWS_ABSOLUTE = Regex("([A-Za-z]:)?[\\\\/].*")

// A missing file is the normal state before the server has written it, so only an unreadable or malformed one is reported.
private fun <T : Any> readJsonFile(file: Path, warn: (String) -> Unit, read: (JsonObject) -> T?): T? = try {
    read(JsonParser.parseString(Files.readString(file)).asJsonObject)
} catch (e: NoSuchFileException) {
    null
} catch (e: Exception) {
    warn("Ignoring $file: ${e.message ?: e.javaClass.simpleName}")
    null
}

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

fun recordedPython(home: Path, windows: Boolean, warn: (String) -> Unit = {}): String? {
    val python = readJsonFile(home.resolve("mcp").resolve("python.json"), warn) { it.get("python")?.asString }
    val absolute = python != null && (if (windows) WINDOWS_ABSOLUTE.matches(python) else python.startsWith("/"))
    if (python != null && absolute && CODEX_PYTHON_SAFE.matches(python) && runCatching { Files.isRegularFile(Path.of(python)) }.getOrDefault(false)) {
        return python
    }
    return null
}

fun codexPython(home: Path, windows: Boolean, warn: (String) -> Unit = {}): List<String> =
    recordedPython(home, windows, warn)?.let { listOf(it) } ?: if (windows) listOf("py", "-3") else listOf("python3")

data class CodexHttp(val python: String, val headers: String, val port: Int, val tab: String, val agent: String, val home: String)

private const val PORT_FILE = "port.json"

// The helper command goes into a TOML literal string and through PowerShell 5.1 and cmd.exe, which mangle a quote, a
// space or a % in an argument, so a path with one leaves the tab on the stdio server.
private val HTTP_SAFE = Regex("[A-Za-z0-9_./:\\\\+-]+")
private val HTTP_WORD = Regex("[A-Za-z0-9._-]{1,128}")

fun codexHttpArg(http: CodexHttp): String {
    val helper = "${http.python} -I -S ${http.headers} --tab ${http.tab} --agent ${http.agent} --port ${http.port} --home ${http.home}"
    return "mcp_servers.ide-agent-tabs={ url = 'http://127.0.0.1:${http.port}/mcp', http_headers_helper = '$helper', " +
        "startup_timeout_sec = 30, tool_timeout_sec = 660 }"
}

fun withCodexHttp(args: List<String>, http: CodexHttp?): List<String>? {
    if (http == null || CODEX_SERVER_ARG !in args) return null
    val arg = codexHttpArg(http)
    return args.map { if (it == CODEX_SERVER_ARG) arg else it }
}

private fun readPort(home: Path, warn: (String) -> Unit): Int? = readJsonFile(home.resolve("mcp").resolve(PORT_FILE), warn) {
    val port = it.get("port").asJsonPrimitive
    if (port.isNumber) port.asString.toIntOrNull()?.takeIf { number -> number in 1..65535 } else null
}

fun codexHttp(home: Path, windows: Boolean, tab: String, agent: String, warn: (String) -> Unit = {}): CodexHttp? {
    val python = recordedPython(home, windows)
    val port = readPort(home, warn)
    val file = home.resolve("mcp").resolve("py").resolve("launch").resolve("headers.py")
    val headers = if (windows) file.toString().replace('\\', '/') else file.toString()
    if (python == null || port == null || !runCatching { Files.isRegularFile(file) }.getOrDefault(false)) return null
    if (!HTTP_WORD.matches(tab) || !HTTP_WORD.matches(agent)) return null
    val homePath = home.toString()
    if (listOf(python, headers, homePath).any { !HTTP_SAFE.matches(it) }) return null
    return CodexHttp(python, headers, port, tab, agent, homePath)
}

// Same rules as copilot_http and with_copilot_http in claude-plugin/mcp/src/ide_agent_tabs/profiles.py.
data class CopilotHttp(val config: String, val port: Int)

const val CWD_ENV = "IDE_AGENT_TABS_CWD"
const val COPILOT_HEALTH_TIMEOUT_MS = 500
private const val COPILOT_FILE = "copilot-http.json"
private const val COPILOT_HOOKS_FILE = "ide-agent-tabs.json"
private const val MAX_CWD_CHARS = 1024
private val COPILOT_COMMAND = Regex("(?-u)(?:.*[\\\\/])?copilot(?:\\.(?:exe|cmd|bat|ps1))?", RegexOption.IGNORE_CASE)
private val CWD_SAFE = Regex("[A-Za-z0-9_.~/: \\\\-]")
private val COPILOT_URL = Regex("http://127\\.0\\.0\\.1:([0-9]{1,5})/mcp")

fun isCopilotCommand(command: String): Boolean = COPILOT_COMMAND.matches(command)

fun cwdHeader(cwd: String): String = buildString {
    var i = 0
    while (i < cwd.length) {
        val char = String(Character.toChars(cwd.codePointAt(i)))
        if (CWD_SAFE.matches(char)) append(char) else char.toByteArray(Charsets.UTF_8).forEach { append("%%%02X".format(it.toInt() and 0xff)) }
        i += char.length
    }
}

private fun copilotConfigPort(home: Path, warn: (String) -> Unit): Int? = readJsonFile(home.resolve("mcp").resolve(COPILOT_FILE), warn) {
    val url = it.getAsJsonObject("mcpServers").getAsJsonObject("ide-agent-tabs").get("url").asJsonPrimitive
    if (url.isString) COPILOT_URL.matchEntire(url.asString)?.groupValues?.get(1)?.toIntOrNull()?.takeIf { port -> port in 1..65535 } else null
}

fun answersHealth(port: Int): Boolean = runCatching {
    val connection = URI("http://127.0.0.1:$port/health").toURL().openConnection(Proxy.NO_PROXY) as HttpURLConnection
    connection.connectTimeout = COPILOT_HEALTH_TIMEOUT_MS
    connection.readTimeout = COPILOT_HEALTH_TIMEOUT_MS
    try {
        val service = connection.takeIf { it.responseCode == 200 }?.inputStream?.bufferedReader()?.use { it.readText() }
            ?.let { JsonParser.parseString(it).asJsonObject.get("service") }
        service != null && service.isJsonPrimitive && service.asString == "ide-agent-tabs"
    } finally {
        connection.disconnect()
    }
}.getOrDefault(false)

fun copilotHooksFile(copilotHome: String?, userHome: String): File =
    File(File(copilotHome?.takeIf { it.isNotEmpty() } ?: File(userHome, ".copilot").path, "hooks"), COPILOT_HOOKS_FILE)

fun copilotHttp(
    home: Path,
    windows: Boolean,
    hooksFile: File,
    warn: (String) -> Unit = {},
    answers: (Int) -> Boolean = { answersHealth(it) },
): CopilotHttp? {
    val port = readPort(home, warn) ?: return null
    val file = home.resolve("mcp").resolve(COPILOT_FILE).toString()
    val config = if (windows) file.replace('\\', '/') else file
    if (copilotConfigPort(home, warn) != port || !HTTP_SAFE.matches(config) || !hooksFile.isFile) return null
    return if (answers(port)) CopilotHttp(config, port) else null
}

// Copilot CLI adds the servers in --additional-mcp-config to its own for one run, and an entry with the registered
// server's name replaces it, so the tab starts no stdio server.
fun withCopilotHttp(
    command: String,
    args: List<String>,
    env: Map<String, String>,
    copilot: CopilotHttp?,
    cwd: String?,
): Pair<List<String>, Map<String, String>> {
    if (copilot == null || !isCopilotCommand(command)) return args to env
    val folder = if (cwd != null && cwd.length in 1..MAX_CWD_CHARS) mapOf(CWD_ENV to cwdHeader(cwd)) else emptyMap()
    return (args + listOf("--additional-mcp-config", "@${copilot.config}")) to (env + folder)
}

// Same rules as claude_tab_settings and with_claude_settings in claude-plugin/mcp/src/ide_agent_tabs/profiles.py.
const val CLAUDE_TAB_SETTINGS_FILE = "claude-tab-settings.json"

// The settings path reaches cmd.exe when claude is a .cmd shim, so it may not hold a double quote or a cmd.exe metacharacter.
private val CMD_SAFE_PATH = Regex("[^\"%!^&|<>\\u0000-\\u001f\\u007f]+")
private val CLAUDE_COMMAND = Regex("(?-u)(?:.*[\\\\/])?claude(?:\\.(?:exe|cmd|bat|ps1))?", RegexOption.IGNORE_CASE)

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

private fun readSettings(value: String, cwd: String?, warn: (String) -> Unit): JsonObject? {
    val inline = value.trim().startsWith("{")
    val what = if (inline) "the inline --settings value" else value
    return try {
        val text = if (inline) {
            value
        } else {
            val absolute = value.startsWith("/") || WINDOWS_ABSOLUTE.matches(value)
            if (!absolute && cwd == null) return null
            Files.readString(if (absolute) Path.of(value) else Path.of(cwd!!).resolve(value))
        }
        JsonParser.parseString(text).takeIf { it.isJsonObject }?.asJsonObject
            ?: run {
                warn("Ignoring $what: it is not a JSON object")
                null
            }
    } catch (e: Exception) {
        warn("Ignoring $what: ${e.message ?: e.javaClass.simpleName}")
        null
    }
}

fun mergedClaudeSettings(settings: String, value: String, cwd: String?, warn: (String) -> Unit = {}): String? {
    val user = readSettings(value, cwd, warn) ?: return null
    val ours = readSettings(settings, null, warn)?.get("hooks")?.takeIf { it.isJsonObject }?.asJsonObject ?: return null
    val hooks = user.get("hooks") ?: JsonObject()
    if (!hooks.isJsonObject) {
        warn("Not merging the tab hooks into the --settings value: its hooks entry is not an object")
        return null
    }
    val merged = hooks.asJsonObject.deepCopy()
    for ((event, groups) in ours.entrySet()) {
        val existing = merged.get(event) ?: JsonArray()
        if (!existing.isJsonArray || !groups.isJsonArray) {
            warn("Not merging the tab hooks into the --settings value: hooks.$event is not an array")
            return null
        }
        merged.add(event, existing.asJsonArray.deepCopy().apply { addAll(groups.asJsonArray) })
    }
    val result = user.deepCopy().apply { add("hooks", merged) }
    val text = result.toPrettyText()
    val digest = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }.take(16)
    val file = Path.of(settings).resolveSibling("${CLAUDE_TAB_SETTINGS_FILE.removeSuffix(".json")}-$digest.json")
    return try {
        if (!Files.isRegularFile(file) || Files.readString(file) != text) writeAtomically(file, text, private = true)
        file.toString()
    } catch (e: Exception) {
        warn("Could not write the merged settings $file: ${e.message ?: e.javaClass.simpleName}")
        null
    }
}

// claude reads only the last --settings, so a tab whose profile or request passes its own gets one file that
// merges those settings with the tab's hooks, and keeps the user's flag only when that merge is impossible.
fun withClaudeSettings(
    command: String,
    args: List<String>,
    callerArgs: List<String>,
    settings: String?,
    cwd: String? = null,
    warn: (String) -> Unit = {},
): Pair<List<String>, List<String>> {
    if (settings == null || !CLAUDE_COMMAND.matches(command)) return args to callerArgs
    val (own, ownValue) = withoutSettings(args)
    val (caller, callerValue) = withoutSettings(callerArgs)
    val value = callerValue ?: ownValue ?: return (args + listOf("--settings", settings)) to callerArgs
    val merged = mergedClaudeSettings(settings, value, cwd, warn) ?: return args to callerArgs
    return (own + listOf("--settings", merged)) to caller
}
