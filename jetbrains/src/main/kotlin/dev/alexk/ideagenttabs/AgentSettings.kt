package dev.alexk.ideagenttabs

import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.FileTime

// Pure module: no IDE types, so the settings caches test without a running IDE.

fun readDefaultAgent(text: String): String? {
    val value = parseJsonObject(text, CONFIG_FILE).get("defaultAgent") ?: return null
    return if (value.isJsonPrimitive && value.asJsonPrimitive.isString) value.asString else null
}

fun withDefaultAgent(existing: String?, name: String): String = editConfig(existing) { it.addProperty("defaultAgent", name) }

class AgentSettings(home: Path, private val warn: (String) -> Unit) {

    private class Cached<T>(val stamp: Pair<FileTime, Long>?, val value: T)

    private class Config(val defaultAgent: String?, val shared: SharedSettings)

    private inner class Watched<T>(
        private val file: Path,
        private val ignoring: String,
        private val fallback: T,
        private val parse: (String) -> T,
    ) {

        @Volatile
        private var cached: Cached<T>? = null

        fun get(): T {
            val stamp = stamp(file)
            cached?.takeIf { it.stamp == stamp }?.let { return it.value }
            val value = if (stamp == null) {
                fallback
            } else {
                try {
                    parse(Files.readString(file))
                } catch (e: Exception) {
                    warn("$ignoring: ${e.message}")
                    fallback
                }
            }
            cached = Cached(stamp, value)
            return value
        }

        fun forget() {
            cached = null
        }
    }

    private val agentsFile = home.resolve(AGENTS_FILE)
    private val configFile = home.resolve(CONFIG_FILE)
    private val detectedFile = home.resolve(DETECTED_FILE)

    private val agentsWatch = Watched(agentsFile, "Ignoring $agentsFile and using the built-in agent profiles", BUILTIN_PROFILES) {
        mergeProfiles(BUILTIN_PROFILES, parseProfiles(it))
    }

    private val configWatch = Watched(configFile, "Ignoring $configFile", Config(null, SharedSettings())) {
        Config(readDefaultAgent(it), readSharedSettings(it))
    }

    private val detectedWatch = Watched(detectedFile, "Ignoring $detectedFile", Detected(), ::parseDetected)

    fun profiles(): List<AgentProfile> = agentsWatch.get()

    fun profile(name: String): AgentProfile? = findProfile(profiles(), name)

    fun defaultProfile(): AgentProfile {
        val all = profiles()
        return findProfile(all, configWatch.get().defaultAgent) ?: all.first { it.name == DEFAULT_AGENT }
    }

    fun shared(): SharedSettings = configWatch.get().shared

    fun detected(): Detected = detectedWatch.get()

    fun setDefaultAgent(name: String): Boolean = save("the default agent") { withDefaultAgent(it, name) }

    fun setTabRouting(value: TabRouting): Boolean = saveShared("tabRouting", value.value)

    fun setTerminal(value: String): Boolean = saveShared("terminal", value)

    fun setShell(value: String): Boolean = saveShared("shell", value)

    fun setTerminalWindow(value: TerminalWindow): Boolean = saveShared("terminalWindow", value.value)

    fun setFocusNewTabs(value: FocusNewTabs): Boolean = saveShared("focusNewTabs", value.value)

    fun setClaudeMod(on: Boolean): Boolean = saveShared("claudeMod", if (on) AUTO else CLAUDE_MOD_OFF)

    fun setCloseAfterHandoff(value: Boolean): Boolean = saveFlag("closeAfterHandoff", value, default = true)

    fun setAllowResume(value: Boolean): Boolean = saveFlag("allowResume", value, default = true)

    fun setShowOriAgents(value: Boolean): Boolean = saveFlag("showOriAgents", value, default = false)

    fun setShowLmStudioAgents(value: Boolean): Boolean = saveFlag("showLmStudioAgents", value, default = false)

    fun setShowOllamaAgents(value: Boolean): Boolean = saveFlag("showOllamaAgents", value, default = false)

    private fun saveShared(key: String, value: String): Boolean = save(key) { withSharedValue(it, key, value) }

    private fun saveFlag(key: String, value: Boolean, default: Boolean): Boolean = save(key) { withSharedFlag(it, key, value, default) }

    private fun save(what: String, edit: (String?) -> String): Boolean = try {
        val existing = if (Files.isRegularFile(configFile)) Files.readString(configFile) else null
        writeAtomically(configFile, edit(existing))
        configWatch.forget()
        true
    } catch (e: Exception) {
        warn("Could not save $what to $configFile: ${e.message}")
        false
    }

    private fun stamp(file: Path): Pair<FileTime, Long>? =
        runCatching { Files.getLastModifiedTime(file) to Files.size(file) }.getOrNull()
}
