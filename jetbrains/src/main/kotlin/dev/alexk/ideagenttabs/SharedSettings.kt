package dev.alexk.ideagenttabs

import com.google.gson.GsonBuilder
import com.google.gson.JsonObject
import com.google.gson.JsonParser

// Pure module: no IDE types, so the shared settings test without a running IDE.

const val DETECTED_FILE = "detected.json"
const val AUTO = "auto"
const val CLAUDE_MOD_OFF = "off"

enum class TabRouting(val value: String, val label: String) {
    PROJECT("project", "IDE that has the project open"),
    CALLER("caller", "IDE the request came from"),
    ;

    companion object {
        fun of(value: String?): TabRouting? = entries.firstOrNull { it.value == value }
    }
}

enum class TerminalWindow(val value: String, val label: String) {
    LAST("last", "Use my last window"),
    DEDICATED("dedicated", "A dedicated Agent Tabs window"),
    ;

    companion object {
        fun of(value: String?): TerminalWindow? = entries.firstOrNull { it.value == value }
    }
}

enum class LaunchVia(val value: String, val label: String) {
    DIRECT("direct", "Directly"),
    ORI("ori", "Through Ori"),
    ;

    companion object {
        fun of(value: String?): LaunchVia? = entries.firstOrNull { it.value == value }
    }
}

enum class FocusNewTabs(val value: String, val label: String) {
    AUTO_FOCUS(AUTO, "When you asked for the tab"),
    ALWAYS("always", "Always"),
    NEVER("never", "Never"),
    ;

    companion object {
        fun of(value: String?): FocusNewTabs? = entries.firstOrNull { it.value == value }
    }
}

data class SharedSettings(
    val tabRouting: TabRouting = TabRouting.PROJECT,
    val terminal: String = AUTO,
    val shell: String = AUTO,
    val terminalWindow: TerminalWindow = TerminalWindow.LAST,
    val launchVia: LaunchVia = LaunchVia.DIRECT,
    val closeAfterHandoff: Boolean = true,
    val allowResume: Boolean = true,
    val focusNewTabs: FocusNewTabs = FocusNewTabs.AUTO_FOCUS,
    val claudeMod: Boolean = true,
)

data class DetectedTerminal(val id: String, val name: String)

data class DetectedShell(val path: String, val label: String)

data class DetectedOri(val path: String, val version: String?, val agents: List<String>)

data class Detected(
    val terminals: List<DetectedTerminal> = emptyList(),
    val shells: List<DetectedShell> = emptyList(),
    val ori: DetectedOri? = null,
)

fun readSharedSettings(text: String): SharedSettings {
    val root = parseJsonObject(text, CONFIG_FILE)
    return SharedSettings(
        tabRouting = TabRouting.of(root.stringOrNull("tabRouting")) ?: TabRouting.PROJECT,
        terminal = root.stringOrNull("terminal")?.ifBlank { AUTO } ?: AUTO,
        shell = root.stringOrNull("shell")?.ifBlank { AUTO } ?: AUTO,
        terminalWindow = TerminalWindow.of(root.stringOrNull("terminalWindow")) ?: TerminalWindow.LAST,
        launchVia = LaunchVia.of(root.stringOrNull("launchVia")) ?: LaunchVia.DIRECT,
        closeAfterHandoff = root.booleanOrNull("closeAfterHandoff") ?: true,
        allowResume = root.booleanOrNull("allowResume") ?: true,
        focusNewTabs = FocusNewTabs.of(root.stringOrNull("focusNewTabs")) ?: FocusNewTabs.AUTO_FOCUS,
        claudeMod = root.stringOrNull("claudeMod") != CLAUDE_MOD_OFF,
    )
}

fun withSharedValue(existing: String?, key: String, value: String): String {
    val root = if (existing.isNullOrBlank()) JsonObject() else parseJsonObject(existing, CONFIG_FILE)
    if (value == AUTO || value.isBlank()) root.remove(key) else root.addProperty(key, value)
    return GsonBuilder().setPrettyPrinting().create().toJson(root) + "\n"
}

fun withSharedFlag(existing: String?, key: String, value: Boolean, default: Boolean): String {
    val root = if (existing.isNullOrBlank()) JsonObject() else parseJsonObject(existing, CONFIG_FILE)
    if (value == default) root.remove(key) else root.addProperty(key, value)
    return GsonBuilder().setPrettyPrinting().create().toJson(root) + "\n"
}

fun parseDetected(text: String): Detected {
    val root = parseJsonObject(text, DETECTED_FILE)
    val terminals = root.objects("terminals").mapNotNull { entry ->
        val id = entry.stringOrNull("id")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
        DetectedTerminal(id, entry.stringOrNull("name")?.takeIf { it.isNotEmpty() } ?: id)
    }
    val shells = root.objects("shells").mapNotNull { entry ->
        val path = entry.stringOrNull("path")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
        DetectedShell(path, entry.stringOrNull("label")?.takeIf { it.isNotEmpty() } ?: path)
    }
    return Detected(terminals, shells, parseOri(root))
}

private fun parseOri(root: JsonObject): DetectedOri? {
    val ori = root.get("ori")?.takeIf { it.isJsonObject }?.asJsonObject ?: return null
    val path = ori.stringOrNull("path")?.takeIf { it.isNotEmpty() } ?: return null
    val agents = ori.get("agents")?.takeIf { it.isJsonArray }?.asJsonArray
        ?.filter { it.isJsonPrimitive && it.asJsonPrimitive.isString && it.asString.isNotEmpty() }
        ?.map { it.asString }
        .orEmpty()
    return DetectedOri(path, ori.stringOrNull("version")?.takeIf { it.isNotEmpty() }, agents)
}

private fun JsonObject.stringOrNull(key: String): String? {
    val value = get(key) ?: return null
    return if (value.isJsonPrimitive && value.asJsonPrimitive.isString) value.asString else null
}

private fun JsonObject.booleanOrNull(key: String): Boolean? {
    val value = get(key) ?: return null
    return if (value.isJsonPrimitive && value.asJsonPrimitive.isBoolean) value.asBoolean else null
}

private fun JsonObject.objects(key: String): List<JsonObject> {
    val value = get(key) ?: return emptyList()
    if (!value.isJsonArray) return emptyList()
    return value.asJsonArray.filter { it.isJsonObject }.map { it.asJsonObject }
}
