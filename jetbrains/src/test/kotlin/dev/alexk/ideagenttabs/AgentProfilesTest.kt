package dev.alexk.ideagenttabs

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.FileTime

class AgentProfilesTest {

    private val home: Path = Files.createTempDirectory("cst-agents")
    private val warnings = mutableListOf<String>()
    private val settings = AgentSettings(home) { warnings += it }

    private var clock = System.currentTimeMillis()

    private fun write(name: String, text: String) {
        val file = home.resolve(name)
        Files.writeString(file, text)
        clock += 10_000
        Files.setLastModifiedTime(file, FileTime.fromMillis(clock))
    }

    private fun writeAgents(text: String) = write(AGENTS_FILE, text)

    @Test
    fun `built-in profiles match the design`() {
        assertEquals(listOf("claude", "codex", "agy", "copilot", "gemini"), settings.profiles().map { it.name })
        assertEquals(listOf("Claude Code", "Codex", "Antigravity CLI", "Copilot CLI", "Gemini CLI"), settings.profiles().map { it.label })
        assertEquals(listOf("claude", "codex", "agy", "copilot", "gemini"), settings.profiles().map { it.command })
        assertEquals(listOf(null, null, "-i", "-i", "-i"), settings.profiles().map { it.promptFlag })
        assertEquals(listOf(emptyList(), CODEX_TAB_ARGS, emptyList(), emptyList(), emptyList()), settings.profiles().map { it.args })
        assertEquals(listOf("--no-daemon", "-c"), CODEX_TAB_ARGS.take(2))
        assertEquals("claude", settings.defaultProfile().name)
        assertTrue(warnings.isEmpty())
    }

    @Test
    fun `agents file overrides a built-in by name and adds new profiles`() {
        writeAgents(
            """
            {
              "codex": {"label": "Codex (fast)", "command": "codex", "args": ["--model", "o4"]},
              "opencode-local": {
                "label": "OpenCode (LM Studio)", "command": "opencode",
                "args": ["--model", "lmstudio/qwen3-coder"], "promptFlag": "--prompt",
                "env": {"LMSTUDIO": "1"}, "icon": "icons/opencode.svg"
              },
              "bare": {"command": "bare-cli"}
            }
            """.trimIndent(),
        )
        val profiles = settings.profiles()
        assertEquals(listOf("claude", "codex", "agy", "copilot", "gemini", "opencode-local", "bare"), profiles.map { it.name })
        assertEquals(AgentProfile("codex", "Codex (fast)", "codex", listOf("--model", "o4")), settings.profile("codex"))
        assertEquals(
            AgentProfile("opencode-local", "OpenCode (LM Studio)", "opencode", listOf("--model", "lmstudio/qwen3-coder"), "--prompt", mapOf("LMSTUDIO" to "1"), "icons/opencode.svg"),
            settings.profile("opencode-local"),
        )
        assertEquals("bare", settings.profile("bare")?.label)
        assertNull(settings.profile("absent"))
        assertTrue(warnings.isEmpty())
    }

    @Test
    fun `a bad agents file logs a warning and leaves the built-ins`() {
        val bad = listOf(
            "not json",
            "[]",
            """{"x": "codex"}""",
            """{"x": {}}""",
            """{"x": {"command": ""}}""",
            """{"x": {"command": 1}}""",
            """{"x": {"command": "x", "args": "--a"}}""",
            """{"x": {"command": "x", "args": [1]}}""",
            """{"x": {"command": "x", "promptFlag": " "}}""",
            """{"x": {"command": "x", "env": {"A": 1}}}""",
            """{"x": {"command": "x", "env": {"A=B": "1"}}}""",
            """{"x": {"command": "x", "env": {"IDE_AGENT_TABS_COMMAND": "evil"}}}""",
            """{"x": {"command": "x", "env": {"jediterm_source": "evil"}}}""",
            """{"bad name": {"command": "x"}}""",
            """{"claude": {"command": "x"}, "y": {"command": "y", "args": {}}}""",
        )
        for ((i, text) in bad.withIndex()) {
            writeAgents(text)
            assertEquals(text, BUILTIN_PROFILES, settings.profiles())
            assertEquals(text, i + 1, warnings.size)
        }
    }

    @Test
    fun `the agents file is read again only when it changes`() {
        writeAgents("""{"x": {"command": "x"}}""")
        assertEquals("x", settings.profiles().last().name)
        assertEquals("x", settings.profiles().last().name)
        writeAgents("""{"y": {"command": "y"}}""")
        assertEquals("y", settings.profiles().last().name)
        Files.delete(home.resolve(AGENTS_FILE))
        assertEquals(BUILTIN_PROFILES, settings.profiles())
    }

    @Test
    fun `arguments are profile args, caller args, prompt flag, then the prompt`() {
        val profile = AgentProfile("p", "P", "cli", args = listOf("--model", "m"), promptFlag = "-i")
        val launch = profile.launch("hello", listOf("--yolo"))
        assertEquals("p", launch.agent)
        assertEquals("cli", launch.command)
        assertEquals(listOf("--model", "m", "--yolo", "-i"), launch.args)
        assertEquals("hello", launch.prompt)
    }

    @Test
    fun `no prompt means no prompt flag`() {
        val profile = AgentProfile("p", "P", "cli", args = listOf("--model", "m"), promptFlag = "-i")
        val launch = profile.launch(null, listOf("--yolo"))
        assertEquals(listOf("--model", "m", "--yolo"), launch.args)
        assertNull(launch.prompt)
        assertEquals(listOf<String>(), AgentProfile("claude", "Claude Code", "claude").launch("hi").args)
    }

    @Test
    fun `caller env wins over profile env`() {
        val profile = AgentProfile("p", "P", "cli", env = mapOf("A" to "profile", "B" to "profile"))
        assertEquals(mapOf("A" to "caller", "B" to "profile", "C" to "caller"), profile.launch(null, callerEnv = mapOf("A" to "caller", "C" to "caller")).env)
    }

    @Test
    fun `reserved names are refused in profile env`() {
        for (name in listOf("IDE_AGENT_TABS_ID", "ide_agent_tabs_agent", "JEDITERM_SOURCE", "JEDITERM_SOURCE_ARGS")) {
            assertThrows(name, IllegalArgumentException::class.java) { checkEnv(mapOf(name to "x"), "env") }
        }
        checkEnv(mapOf("CLAUDE_CODE_USE_BEDROCK" to "1"), "env")
    }

    @Test
    fun `default agent comes from config and falls back to claude`() {
        write(CONFIG_FILE, """{"defaultAgent": "gemini"}""")
        assertEquals("gemini", settings.defaultProfile().name)
        write(CONFIG_FILE, """{"defaultAgent": "nope", "other": 1}""")
        assertEquals("claude", settings.defaultProfile().name)
        write(CONFIG_FILE, "broken")
        assertEquals("claude", settings.defaultProfile().name)
        assertEquals(1, warnings.size)
    }

    @Test
    fun `saving the default agent keeps the other settings`() {
        val config = home.resolve(CONFIG_FILE)
        assertTrue(settings.setDefaultAgent("codex"))
        assertEquals("codex", readDefaultAgent(Files.readString(config)))
        assertEquals("codex", settings.defaultProfile().name)

        Files.writeString(config, """{"defaultAgent": "claude", "theme": {"x": [1, 2]}, "flag": true}""")
        settings.setDefaultAgent("copilot")
        val saved = JsonParser.parseString(Files.readString(config)).asJsonObject
        assertEquals("copilot", saved.get("defaultAgent").asString)
        assertEquals(JsonParser.parseString("""{"x": [1, 2]}"""), saved.get("theme"))
        assertTrue(saved.get("flag").asBoolean)
        assertEquals(listOf(CONFIG_FILE), Files.list(home).use { s -> s.map { it.fileName.toString() }.toList() })
    }

    @Test
    fun `saving the default agent leaves a broken config alone`() {
        val config = home.resolve(CONFIG_FILE)
        Files.writeString(config, "{broken")
        assertFalse(settings.setDefaultAgent("codex"))
        assertEquals("{broken", Files.readString(config))
        assertEquals(1, warnings.size)
    }

    @Test
    fun `saving the default agent reports a config file it cannot write`() {
        Files.createDirectory(home.resolve(CONFIG_FILE))
        assertFalse(settings.setDefaultAgent("codex"))
        assertEquals(1, warnings.size)
    }

    @Test
    fun `installed means the command is on PATH, with Windows extensions on Windows`() {
        val bin = Files.createDirectories(home.resolve("bin"))
        Files.createFile(bin.resolve("posix-cli"))
        Files.createFile(bin.resolve("npm-cli.cmd"))
        Files.createFile(bin.resolve("shim-cli.ps1"))
        Files.createFile(bin.resolve("native-cli.exe"))
        Files.createFile(bin.resolve("old-cli.bat"))
        val path = listOf(home.resolve("missing").toString(), bin.toString()).joinToString(File.pathSeparator)
        for (command in listOf("posix-cli", "npm-cli", "shim-cli", "native-cli", "old-cli", "native-cli.exe")) {
            assertTrue(command, isInstalled(command, path, isWindows = true))
        }
        assertTrue(isInstalled("posix-cli", path, isWindows = false))
        assertFalse(isInstalled("npm-cli", path, isWindows = false))
        assertFalse(isInstalled("absent", path, isWindows = true))
        assertTrue(isInstalled(bin.resolve("posix-cli").toString(), "", isWindows = false))
        assertTrue(isInstalled(bin.resolve("npm-cli").toString(), "", isWindows = true))
        assertFalse(isInstalled(bin.resolve("absent").toString(), path, isWindows = false))
        assertFalse(isInstalled("bin${File.separator}posix-cli", path, isWindows = false))
    }
}
