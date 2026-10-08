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
        assertEquals(
            listOf("claude", "codex", "agy", "copilot", "gemini", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex-local"),
            settings.profiles().map { it.name },
        )
        assertEquals(
            listOf("Claude Code", "Codex", "Antigravity CLI", "Copilot CLI", "Gemini CLI", "Grok Build", "Pi", "Hermes", "OpenCode", "Qwen Code", "Goose", "Codex (local)"),
            settings.profiles().map { it.label },
        )
        assertEquals(
            listOf("claude", "codex", "agy", "copilot", "gemini", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex"),
            settings.profiles().map { it.command },
        )
        assertEquals(
            listOf(null, null, "-i", "-i", "-i", null, null, "-q", "--prompt", "-i", "-t", null),
            settings.profiles().map { it.promptFlag },
        )
        assertEquals(
            listOf(
                emptyList(), CODEX_TAB_ARGS, emptyList(), emptyList(), emptyList(), emptyList(), emptyList(),
                listOf("chat"), emptyList(), emptyList(), listOf("run", "-s"), CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "ollama"),
            ),
            settings.profiles().map { it.args },
        )
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
        assertEquals(
            listOf("claude", "codex", "agy", "copilot", "gemini", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex-local", "opencode-local", "bare"),
            profiles.map { it.name },
        )
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

    @Test
    fun `built-in profiles carry the model flag of each CLI`() {
        assertEquals(
            listOf(
                "claude" to "--model", "codex" to "-m", "agy" to "--model", "copilot" to "--model", "gemini" to "-m", "grok" to "-m", "pi" to "--model",
                "hermes" to "-m", "opencode" to "-m", "qwen" to "-m", "goose" to "--model", "codex-local" to "-m",
            ),
            BUILTIN_PROFILES.map { it.name to it.modelFlag },
        )
    }

    @Test
    fun `a custom profile sets modelFlag in the agents file`() {
        writeAgents("""{"mine": {"command": "mine-cli", "modelFlag": "--use"}, "codex": {"command": "codex"}}""")
        assertEquals("--use", settings.profile("mine")?.modelFlag)
        assertNull(settings.profile("codex")?.modelFlag)
        assertTrue(warnings.isEmpty())
        writeAgents("""{"mine": {"command": "mine-cli", "modelFlag": " "}}""")
        assertEquals(BUILTIN_PROFILES, settings.profiles())
        writeAgents("""{"mine": {"command": "mine-cli", "modelFlag": 1}}""")
        assertEquals(BUILTIN_PROFILES, settings.profiles())
        assertEquals(2, warnings.size)
    }

    private val oriAll = DetectedOri("/home/u/.local/bin/ori", "0.14.3", listOf("claude", "codex"))
    private val claude = AgentProfile("claude", "Claude Code", "claude", modelFlag = "--model")
    private val codex = AgentProfile("codex", "Codex", "codex", args = listOf("--no-daemon"), modelFlag = "-m")
    private val gemini = AgentProfile("gemini", "Gemini CLI", "gemini", promptFlag = "-i", modelFlag = "-m")
    private val bare = AgentProfile("bare", "Bare", "bare-cli", promptFlag = "-i")

    private fun ctx(
        prompt: String? = null,
        args: List<String> = emptyList(),
        env: Map<String, String> = emptyMap(),
        model: String? = null,
        via: LaunchVia? = null,
        setting: LaunchVia = LaunchVia.DIRECT,
        ori: DetectedOri? = oriAll,
        windows: Boolean = false,
        searchPath: String = "",
    ) = LaunchContext(prompt, args, env, model, via, setting, ori, windows, searchPath)

    @Test
    fun `a direct launch puts the model flag after the profile args`() {
        val launch = planLaunch(codex, ctx(model = "gpt-5", args = listOf("--yolo"), prompt = "hi"))
        assertEquals(LaunchVia.DIRECT, launch.via)
        assertEquals("codex", launch.command)
        assertEquals(listOf("--no-daemon", "-m", "gpt-5", "--yolo"), launch.args)
        val withFlag = planLaunch(gemini, ctx(model = "gemini-2.5-pro", prompt = "hi"))
        assertEquals(listOf("-m", "gemini-2.5-pro", "-i"), withFlag.args)
        assertEquals("hi", withFlag.prompt)
        assertEquals(emptyList<String>(), planLaunch(claude, ctx()).args)
    }

    @Test
    fun `Goose starts with goose run -s -t for a first message and goose session without one`() {
        val goose = BUILTIN_PROFILES.first { it.name == "goose" }
        val withPrompt = planLaunch(goose, ctx(prompt = "hi", model = "m1"))
        assertEquals(listOf("goose", "run", "-s", "--model", "m1", "-t", "hi"), listOf(withPrompt.command) + withPrompt.args + listOfNotNull(withPrompt.prompt))
        val empty = planLaunch(goose, ctx(model = "m1"))
        assertEquals(listOf("goose", "session", "--model", "m1"), listOf(empty.command) + empty.args)
        assertEquals(listOf("session"), planLaunch(goose, ctx(setting = LaunchVia.ORI)).args)
        assertTrue(assertThrows(IllegalArgumentException::class.java) { planLaunch(goose, ctx(via = LaunchVia.ORI)) }.message!!.startsWith("goose can't launch through Ori"))
        assertEquals(listOf("run", "-s", "--debug"), planLaunch(goose.copy(args = listOf("run", "-s", "--debug")), ctx()).args)
        assertEquals(listOf("run", "-s"), planLaunch(goose.copy(command = "goose-cli"), ctx()).args)
    }

    @Test
    fun `a model for a profile without modelFlag fails, never silently`() {
        val message = "bare has no model option; open it without model, or set modelFlag for it in agents.json"
        assertEquals(message, assertThrows(IllegalArgumentException::class.java) { planLaunch(bare, ctx(model = "m")) }.message)
        assertEquals(message, assertThrows(IllegalArgumentException::class.java) { planLaunch(bare, ctx(model = "m", via = LaunchVia.DIRECT)) }.message)
        assertEquals(emptyList<String>(), planLaunch(bare, ctx()).args)
    }

    @Test
    fun `an Ori launch runs ori with the agent, the model, then the profile args`() {
        val launch = planLaunch(codex, ctx(via = LaunchVia.ORI, model = "openai/gpt-5", args = listOf("--yolo"), prompt = "hi", env = mapOf("A" to "1")))
        assertEquals(LaunchVia.ORI, launch.via)
        assertEquals("codex", launch.agent)
        assertEquals("ori", launch.command)
        assertEquals(listOf("codex", "--model", "openai/gpt-5", "--no-daemon", "--yolo"), launch.args)
        assertEquals("hi", launch.prompt)
        assertEquals(mapOf("A" to "1"), launch.env)
        assertEquals(listOf("claude"), planLaunch(claude, ctx(via = LaunchVia.ORI)).args)
    }

    @Test
    fun `an Ori launch needs no modelFlag on the profile`() {
        val noFlag = AgentProfile("claude", "Claude Code", "claude")
        val launch = planLaunch(noFlag, ctx(via = LaunchVia.ORI, model = "anthropic/claude-sonnet-4.5"))
        assertEquals(listOf("claude", "--model", "anthropic/claude-sonnet-4.5"), launch.args)
    }

    @Test
    fun `an explicit via beats the setting in both directions`() {
        assertEquals(LaunchVia.ORI, planLaunch(claude, ctx(setting = LaunchVia.ORI)).via)
        assertEquals(LaunchVia.DIRECT, planLaunch(claude, ctx(setting = LaunchVia.ORI, via = LaunchVia.DIRECT)).via)
        assertEquals(LaunchVia.ORI, planLaunch(claude, ctx(setting = LaunchVia.DIRECT, via = LaunchVia.ORI)).via)
        assertEquals(LaunchVia.DIRECT, planLaunch(claude, ctx(setting = LaunchVia.DIRECT)).via)
    }

    @Test
    fun `the setting falls back to a direct launch when Ori cannot run the agent`() {
        val cases = listOf(
            claude to ctx(setting = LaunchVia.ORI, ori = null),
            gemini to ctx(setting = LaunchVia.ORI),
            AgentProfile("grok", "Grok", "grok") to ctx(setting = LaunchVia.ORI),
            AgentProfile("pi", "Pi", "pi") to ctx(setting = LaunchVia.ORI, ori = DetectedOri("/x/ori", null, listOf("claude"))),
        )
        for ((profile, context) in cases) {
            val launch = planLaunch(profile, context)
            assertEquals(profile.name, LaunchVia.DIRECT, launch.via)
            assertEquals(profile.command, launch.command)
        }
        assertEquals(listOf("-m", "gemini-2.5-pro"), planLaunch(gemini, ctx(setting = LaunchVia.ORI, model = "gemini-2.5-pro")).args)
        assertThrows(IllegalArgumentException::class.java) { planLaunch(bare, ctx(setting = LaunchVia.ORI, model = "m")) }
    }

    @Test
    fun `an explicit Ori launch that cannot run fails with the reason`() {
        fun message(profile: AgentProfile, context: LaunchContext) =
            assertThrows(IllegalArgumentException::class.java) { planLaunch(profile, context) }.message
        assertEquals("claude can't launch through Ori: Ori is not installed", message(claude, ctx(via = LaunchVia.ORI, ori = null)))
        assertEquals("gemini can't launch through Ori: Ori does not support gemini", message(gemini, ctx(via = LaunchVia.ORI)))
        assertEquals(
            "pi can't launch through Ori: Ori does not list pi as launchable",
            message(AgentProfile("pi", "Pi", "pi"), ctx(via = LaunchVia.ORI, ori = DetectedOri("/x/ori", null, listOf("claude")))),
        )
    }

    private fun shimDir(vararg files: String): String {
        val bin = Files.createTempDirectory("cst-shim")
        for (name in files) Files.writeString(bin.resolve(name), "")
        return bin.toString()
    }

    @Test
    fun `on Windows, a cmd shim agent refuses Ori arguments with the characters Ori rejects`() {
        val bin = shimDir("codex.cmd", "claude.exe")
        for (bad in listOf("a|b", "say \"hi\"", "50%", "a^b", "a&b", "<x", "x>")) {
            val own = ctx(via = LaunchVia.ORI, windows = true, searchPath = bin, prompt = bad)
            assertTrue(bad, assertThrows(bad, IllegalArgumentException::class.java) { planLaunch(codex, own) }.message!!.contains(".cmd shim"))
            assertThrows(bad, IllegalArgumentException::class.java) { planLaunch(codex, ctx(via = LaunchVia.ORI, windows = true, searchPath = bin, args = listOf(bad))) }
            assertThrows(bad, IllegalArgumentException::class.java) { planLaunch(codex, ctx(via = LaunchVia.ORI, windows = true, searchPath = bin, model = bad)) }
            assertEquals("claude.exe takes $bad", LaunchVia.ORI, planLaunch(claude, own).via)
            assertEquals("non-Windows takes $bad", LaunchVia.ORI, planLaunch(codex, ctx(via = LaunchVia.ORI, searchPath = bin, prompt = bad)).via)
        }
        assertEquals(LaunchVia.ORI, planLaunch(codex, ctx(via = LaunchVia.ORI, windows = true, searchPath = bin, prompt = "plain text")).via)
    }

    @Test
    fun `on Windows, the setting falls back to a direct launch when a cmd shim refuses an argument`() {
        val bin = shimDir("codex.cmd")
        val launch = planLaunch(codex, ctx(setting = LaunchVia.ORI, windows = true, searchPath = bin, prompt = "say \"hi\""))
        assertEquals(LaunchVia.DIRECT, launch.via)
        assertEquals("codex", launch.command)
    }

    @Test
    fun `the Codex tab arguments hold characters a cmd shim refuses, so Codex falls back or fails on Windows`() {
        val real = BUILTIN_PROFILES.first { it.name == "codex" }
        assertTrue(real.args.any { Regex("[|\"%^&<>]").containsMatchIn(it) })
        val bin = shimDir("codex.cmd")
        assertEquals(LaunchVia.DIRECT, planLaunch(real, ctx(setting = LaunchVia.ORI, windows = true, searchPath = bin)).via)
        val error = assertThrows(IllegalArgumentException::class.java) { planLaunch(real, ctx(via = LaunchVia.ORI, windows = true, searchPath = bin)) }
        assertTrue(error.message!!.contains("can't launch through Ori"))
        assertEquals(LaunchVia.ORI, planLaunch(real, ctx(via = LaunchVia.ORI)).via)
    }

    @Test
    fun `a Codex tab runs its server on the interpreter python json records`() {
        val windows = File.separatorChar == '\\'
        assertEquals(if (windows) listOf("py", "-3") else listOf("python3"), codexPython(home, windows))
        val python = home.resolve("python.exe")
        Files.writeString(python, "")
        Files.createDirectories(home.resolve("mcp"))
        Files.writeString(home.resolve("mcp").resolve("python.json"), JsonParser.parseString("{}").asJsonObject.apply { addProperty("python", python.toString()) }.toString())
        assertEquals(listOf(python.toString()), codexPython(home, windows))
        Files.writeString(home.resolve("mcp").resolve("python.json"), JsonParser.parseString("{}").asJsonObject.apply { addProperty("python", "$python'x") }.toString())
        assertEquals(listOf("py", "-3"), codexPython(home, true))
        assertEquals(listOf("python3"), codexPython(home, false))

        val args = withCodexPython(CODEX_TAB_ARGS, listOf("py", "-3"))
        assertEquals(CODEX_TAB_ARGS.size, args.size)
        assertTrue(args[2].startsWith("mcp_servers.ide-agent-tabs={ command = 'py', args = ['-3', '-I', '-S', '-c', '''"))
        assertEquals(CODEX_TAB_ARGS[2].substringAfter("'-I'"), args[2].substringAfter("'-I'"))
        assertEquals(CODEX_TAB_ARGS.filterIndexed { i, _ -> i != 2 }, args.filterIndexed { i, _ -> i != 2 })
        assertEquals(CODEX_TAB_ARGS, withCodexPython(CODEX_TAB_ARGS, null))
        val plan = planLaunch(BUILTIN_PROFILES[1], LaunchContext(python = listOf("/usr/bin/python3")))
        assertTrue(plan.args[2].contains("command = '/usr/bin/python3', args = ['-I'"))
    }
}
