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

class AgentProfilesTest : SettingsFixture("cst-agents") {

    private fun writeAgents(text: String) = write(AGENTS_FILE, text)

    @Test
    fun `built-in profiles match the design`() {
        assertEquals(
            listOf("claude", "codex", "agy", "copilot", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex-lmstudio", "codex-ollama"),
            settings.profiles().map { it.name },
        )
        assertEquals(
            listOf(
                "Claude Code", "Codex", "Antigravity CLI", "Copilot CLI", "Grok Build", "Pi", "Hermes", "OpenCode", "Qwen Code", "Goose",
                "Codex - LM Studio", "Codex - Ollama",
            ),
            settings.profiles().map { it.label },
        )
        assertEquals(
            listOf("claude", "codex", "agy", "copilot", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex", "codex"),
            settings.profiles().map { it.command },
        )
        assertEquals(
            listOf(null, null, "-i", "-i", null, null, "-q", "--prompt", "-i", "-t", null, null),
            settings.profiles().map { it.promptFlag },
        )
        assertEquals(
            listOf(
                emptyList(), CODEX_TAB_ARGS, emptyList(), emptyList(), emptyList(), emptyList(),
                listOf("chat"), emptyList(), emptyList(), listOf("run", "-s"),
                CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "lmstudio"), CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "ollama"),
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
            listOf(
                "claude", "codex", "agy", "copilot", "grok", "pi", "hermes", "opencode", "qwen", "goose", "codex-lmstudio", "codex-ollama", "opencode-local",
                "bare",
            ),
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
            """{"x\n": {"command": "x"}}""",
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
        assertEquals(listOf<String>(), claude.launch("hi").args)
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
        write(CONFIG_FILE, """{"defaultAgent": "copilot"}""")
        assertEquals("copilot", settings.defaultProfile().name)
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
                "claude" to "--model", "codex" to "-m", "agy" to "--model", "copilot" to "--model", "grok" to "-m", "pi" to "--model",
                "hermes" to "-m", "opencode" to "-m", "qwen" to "-m", "goose" to "--model", "codex-lmstudio" to "-m",
                "codex-ollama" to "-m",
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
    private val codex = AgentProfile("codex", "Codex", "codex", args = listOf("--no-daemon"), modelFlag = "-m")
    private val qwen = AgentProfile("qwen", "Qwen Code", "qwen", promptFlag = "-i", modelFlag = "-m")
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
    fun `agent entries put an Ori entry under each agent Ori can launch, only when shown`() {
        val all = listOf(claude, qwen, codex, AgentProfile("copilot", "Copilot CLI", "copilot"))
        val ori = DetectedOri("/x/ori", "0.14.3", listOf("claude", "codex", "qwen"))
        val installed = { p: AgentProfile -> p.name != "codex" }
        fun names(shown: Boolean, detected: DetectedOri? = ori) = agentEntries(all, detected, SharedSettings(showOriAgents = shown), installed).map { it.name }
        assertEquals(listOf("claude", "qwen", "copilot"), names(false))
        assertEquals(listOf("claude", "ori:claude", "qwen", "ori:codex", "copilot"), names(true))
        assertEquals(listOf("claude", "qwen", "copilot"), names(true, null))
        assertEquals(AgentEntry("ori:claude", "Claude Code - Ori", claude, LaunchVia.ORI), agentEntries(all, ori, SharedSettings(showOriAgents = true), installed)[1])
    }

    @Test
    fun `agent entries put the local Codex rows under Codex after its Ori entry, each only when its toggle is on`() {
        val ori = DetectedOri("/x/ori", "0.14.3", listOf("claude", "codex"))
        val installed = { p: AgentProfile -> p.command == "codex" }
        fun names(toggles: SharedSettings) = agentEntries(BUILTIN_PROFILES, ori, toggles, installed).map { it.name }
        assertEquals(listOf("codex"), names(SharedSettings()))
        assertEquals(listOf("codex", "codex-lmstudio"), names(SharedSettings(showLmStudioAgents = true)))
        assertEquals(listOf("codex", "codex-ollama"), names(SharedSettings(showOllamaAgents = true)))
        assertEquals(
            listOf("ori:claude", "codex", "ori:codex", "codex-lmstudio", "codex-ollama"),
            names(SharedSettings(showOriAgents = true, showLmStudioAgents = true, showOllamaAgents = true)),
        )
        val both = SharedSettings(showLmStudioAgents = true, showOllamaAgents = true)
        assertEquals(emptyList<AgentEntry>(), agentEntries(BUILTIN_PROFILES, null, both, { false }))
        assertEquals(
            listOf(Triple("codex-lmstudio", "Codex - LM Studio", "lmstudio"), Triple("codex-ollama", "Codex - Ollama", "ollama")),
            agentEntries(BUILTIN_PROFILES, null, both, installed).drop(1).map { Triple(it.name, it.label, it.profile.args.last()) },
        )
        assertTrue(agentEntries(BUILTIN_PROFILES, null, both, installed).all { it.via == null })
    }

    @Test
    fun `codex-local finds the Ollama profile unless a profile has that name, and a custom profile overrides a local row in place`() {
        assertEquals("codex-ollama", findProfile(BUILTIN_PROFILES, "codex-local")?.name)
        assertNull(findProfile(BUILTIN_PROFILES, "nothing"))
        write(CONFIG_FILE, """{"defaultAgent": "codex-local"}""")
        assertEquals("codex-ollama", settings.profile("codex-local")?.name)
        assertEquals("codex-ollama", settings.defaultProfile().name)
        writeAgents("""{"codex-ollama": {"label": "My Ollama", "command": "codex", "args": ["--oss"]}, "codex-local": {"command": "old-local"}}""")
        assertEquals("old-local", settings.profile("codex-local")?.command)
        assertEquals("old-local", settings.defaultProfile().command)
        val rows = agentEntries(settings.profiles(), null, SharedSettings(showOllamaAgents = true)) { true }.map { it.name to it.label }
        assertEquals(listOf("codex" to "Codex", "codex-ollama" to "My Ollama", "agy" to "Antigravity CLI"), rows.subList(1, 4))
        assertEquals("codex-local" to "codex-local", rows.last())
    }

    @Test
    fun `a direct launch puts the model flag after the profile args`() {
        val launch = planLaunch(codex, ctx(model = "gpt-5", args = listOf("--yolo"), prompt = "hi"))
        assertEquals(LaunchVia.DIRECT, launch.via)
        assertEquals("codex", launch.command)
        assertEquals(listOf("--no-daemon", "-m", "gpt-5", "--yolo"), launch.args)
        val withFlag = planLaunch(qwen, ctx(model = "qwen3-coder", prompt = "hi"))
        assertEquals(listOf("-m", "qwen3-coder", "-i"), withFlag.args)
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
        val noFlag = claude.copy(modelFlag = null)
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
    fun `planVia reads the route planLaunch picks, without planLaunch's file reads`() {
        val bin = shimDir("codex.cmd")
        val builtin = { name: String -> BUILTIN_PROFILES.first { it.name == name } }
        val contexts = listOf(
            ctx(),
            ctx(setting = LaunchVia.ORI),
            ctx(setting = LaunchVia.ORI, ori = null),
            ctx(setting = LaunchVia.ORI, windows = true, searchPath = bin),
            ctx(setting = LaunchVia.ORI, ori = DetectedOri("/x/ori", null, listOf("claude"))),
        )
        for (profile in listOf(claude, qwen, codex, bare, builtin("codex"), builtin("goose"))) {
            for (context in contexts) {
                assertEquals("${profile.name} ${context.setting} ${context.windows}", planLaunch(profile, context).via, planVia(profile, context))
            }
        }
    }

    @Test
    fun `the setting falls back to a direct launch when Ori cannot run the agent`() {
        val cases = listOf(
            claude to ctx(setting = LaunchVia.ORI, ori = null),
            qwen to ctx(setting = LaunchVia.ORI),
            AgentProfile("grok", "Grok", "grok") to ctx(setting = LaunchVia.ORI),
            AgentProfile("pi", "Pi", "pi") to ctx(setting = LaunchVia.ORI, ori = DetectedOri("/x/ori", null, listOf("claude"))),
        )
        for ((profile, context) in cases) {
            val launch = planLaunch(profile, context)
            assertEquals(profile.name, LaunchVia.DIRECT, launch.via)
            assertEquals(profile.command, launch.command)
        }
        assertEquals(listOf("-m", "qwen3-coder"), planLaunch(qwen, ctx(setting = LaunchVia.ORI, model = "qwen3-coder")).args)
        assertThrows(IllegalArgumentException::class.java) { planLaunch(bare, ctx(setting = LaunchVia.ORI, model = "m")) }
    }

    @Test
    fun `an explicit Ori launch that cannot run fails with the reason`() {
        fun message(profile: AgentProfile, context: LaunchContext) =
            assertThrows(IllegalArgumentException::class.java) { planLaunch(profile, context) }.message
        assertEquals("claude can't launch through Ori: Ori is not installed", message(claude, ctx(via = LaunchVia.ORI, ori = null)))
        assertEquals("qwen can't launch through Ori: Ori does not support qwen", message(qwen, ctx(via = LaunchVia.ORI)))
        assertEquals(
            "pi can't launch through Ori: Ori does not list pi as launchable",
            message(AgentProfile("pi", "Pi", "pi"), ctx(via = LaunchVia.ORI, ori = DetectedOri("/x/ori", null, listOf("claude")))),
        )
    }

    private fun shimDir(vararg files: String): String {
        val bin = temp.create("cst-shim")
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
        assertEquals(if (IS_WINDOWS) listOf("py", "-3") else listOf("python3"), codexPython(home, IS_WINDOWS))
        val python = home.resolve("python.exe")
        Files.writeString(python, "")
        Files.createDirectories(home.resolve("mcp"))
        Files.writeString(home.resolve("mcp").resolve("python.json"), JsonParser.parseString("{}").asJsonObject.apply { addProperty("python", python.toString()) }.toString())
        assertEquals(listOf(python.toString()), codexPython(home, IS_WINDOWS))
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

    private fun httpHome(port: Int? = 47828, stub: Boolean = true, python: Path? = null, prefix: String = "cst-http"): Path {
        val dir = temp.create(prefix)
        val mcp = Files.createDirectories(dir.resolve("mcp"))
        val interpreter = python ?: dir.resolve("python.exe").also { Files.writeString(it, "") }
        Files.writeString(mcp.resolve("python.json"), JsonParser.parseString("{}").asJsonObject.apply { addProperty("python", interpreter.toString()) }.toString())
        if (port != null) Files.writeString(mcp.resolve("port.json"), """{"port": $port}""")
        if (stub) Files.writeString(Files.createDirectories(mcp.resolve("py").resolve("launch")).resolve("headers.py"), "")
        return dir
    }

    @Test
    fun `a Codex tab uses the shared server when the copy, the port and the interpreter are recorded`() {
        val dir = httpHome(port = 47831)
        val http = codexHttp(dir, IS_WINDOWS, "tab-1", "codex")!!
        val headers = dir.resolve("mcp").resolve("py").resolve("launch").resolve("headers.py").toString().replace('\\', '/')
        assertEquals(CodexHttp(dir.resolve("python.exe").toString(), headers, 47831, "tab-1", "codex", dir.toString()), http)
        assertEquals(
            "mcp_servers.ide-agent-tabs={ url = 'http://127.0.0.1:47831/mcp', http_headers_helper = '${http.python} -I -S $headers " +
                "--tab tab-1 --agent codex --port 47831 --home $dir', startup_timeout_sec = 30, tool_timeout_sec = 660 }",
            codexHttpArg(http),
        )
    }

    @Test
    fun `a Codex tab stays on the stdio server without a valid port, a headers stub or a safe path`() {
        assertNull(codexHttp(httpHome(port = null), IS_WINDOWS, "tab-1", "codex"))
        assertNull(codexHttp(httpHome(stub = false), IS_WINDOWS, "tab-1", "codex"))
        assertNull(codexHttp(httpHome(), IS_WINDOWS, "tab 1", "codex"))
        assertNull(codexHttp(httpHome(), IS_WINDOWS, "tab-1", "co dex"))
        assertNull(codexHttp(httpHome(), IS_WINDOWS, "a".repeat(129), "codex"))
        val spaced = Files.createDirectories(temp.create("cst-sp").resolve("sp ace"))
        assertNull(codexHttp(httpHome(python = spaced.resolve("python.exe").also { Files.writeString(it, "") }), IS_WINDOWS, "tab-1", "codex"))
        assertNull(codexHttp(httpHome(prefix = "cst-sp ace"), IS_WINDOWS, "tab-1", "codex"))
        for (port in listOf("0", "65536", "-1", "47828.5", "\"47828\"", "true", "null", "[]")) {
            val dir = httpHome()
            Files.writeString(dir.resolve("mcp").resolve("port.json"), """{"port": $port}""")
            assertNull(port, codexHttp(dir, IS_WINDOWS, "tab-1", "codex"))
        }
        val broken = httpHome()
        Files.writeString(broken.resolve("mcp").resolve("port.json"), "not json")
        assertNull(codexHttp(broken, IS_WINDOWS, "tab-1", "codex"))
        assertEquals(65535, codexHttp(httpHome(port = 65535), IS_WINDOWS, "tab-1", "codex")?.port)
    }

    @Test
    fun `withCodexHttp changes only the server argument`() {
        val http = codexHttp(httpHome(), IS_WINDOWS, "tab-1", "codex-ollama")!!
        val swapped = withCodexHttp(CODEX_TAB_ARGS, http)!!
        assertEquals(CODEX_TAB_ARGS.size, swapped.size)
        assertEquals(listOf(codexHttpArg(http)), swapped.filterIndexed { i, arg -> arg != CODEX_TAB_ARGS[i] })
        assertEquals(codexHttpArg(http), swapped[2])
        assertNull(withCodexHttp(CODEX_TAB_ARGS, null))
        assertNull(withCodexHttp(listOf("--other"), http))
        assertFalse(Regex("[\"%!\\n]").containsMatchIn(codexHttpArg(http)))
    }

    @Test
    fun `planLaunch gives a Codex tab the HTTP argument only when an HTTP server is set`() {
        val codex = BUILTIN_PROFILES.first { it.name == "codex" }
        val http = codexHttp(httpHome(), IS_WINDOWS, "tab-1", "codex")!!
        val shared = planLaunch(codex, LaunchContext(http = http, python = listOf("/usr/bin/python3")))
        assertEquals(codexHttpArg(http), shared.args[2])
        assertEquals(CODEX_TAB_ARGS.filterIndexed { i, _ -> i != 2 }, shared.args.filterIndexed { i, _ -> i != 2 })
        val stdio = planLaunch(codex, LaunchContext(python = listOf("/usr/bin/python3")))
        assertTrue(stdio.args[2].contains("command = '/usr/bin/python3', args = ['-I'"))
        assertEquals(CODEX_TAB_ARGS, planLaunch(codex, LaunchContext()).args)
        val claude = BUILTIN_PROFILES.first { it.name == "claude" }
        assertEquals(emptyList<String>(), planLaunch(claude, LaunchContext(http = http)).args)
    }

    private fun hooksOf(dir: Path): File = copilotHooksFile(null, dir.resolve("user").toString())

    private fun copilotHome(
        port: Int? = 47951,
        configPort: Int? = 47951,
        prefix: String = "cst-copilot",
        hooks: Boolean = true,
    ): Path {
        val dir = temp.create(prefix)
        if (hooks) {
            val file = hooksOf(dir)
            file.parentFile.mkdirs()
            file.writeText("{}")
        }
        val mcp = Files.createDirectories(dir.resolve("mcp"))
        if (port != null) Files.writeString(mcp.resolve("port.json"), """{"port": $port}""")
        if (configPort != null) {
            val entry = """{"type": "http", "url": "http://127.0.0.1:$configPort/mcp", "headers": {}, "tools": ["*"]}"""
            Files.writeString(mcp.resolve("copilot-http.json"), """{"mcpServers": {"ide-agent-tabs": $entry}}""")
        }
        return dir
    }

    @Test
    fun `a Copilot tab uses the shared server when the port, the config and a running server agree`() {
        val dir = copilotHome()
        val asked = mutableListOf<Int>()
        val http = copilotHttp(dir, IS_WINDOWS, hooksOf(dir)) { asked += it; true }
        val file = dir.resolve("mcp").resolve("copilot-http.json").toString()
        assertEquals(CopilotHttp(if (IS_WINDOWS) file.replace('\\', '/') else file, 47951), http)
        assertEquals(listOf(47951), asked)
    }

    @Test
    fun `a Copilot tab stays on the stdio server without a server, a port, a matching config or a safe path`() {
        fun tab(dir: Path, answers: (Int) -> Boolean) = copilotHttp(dir, IS_WINDOWS, hooksOf(dir), answers = answers)
        assertNull(tab(copilotHome()) { false })
        assertNull(tab(copilotHome(port = null)) { true })
        assertNull(tab(copilotHome(configPort = null)) { true })
        assertNull(tab(copilotHome(configPort = 47952)) { true })
        assertNull(tab(copilotHome(prefix = "cst-copilot sp ace")) { true })
    }

    @Test
    fun `a Copilot tab stays on the stdio server without the registered hooks file`() {
        val asked = mutableListOf<Int>()
        val dir = copilotHome(hooks = false)
        assertNull(copilotHttp(dir, IS_WINDOWS, hooksOf(dir)) { asked += it; true })
        assertNull(copilotHttp(dir, IS_WINDOWS, hooksOf(dir).parentFile) { asked += it; true })
        assertEquals(emptyList<Int>(), asked)
        hooksOf(dir).apply { parentFile.mkdirs() }.writeText("{}")
        assertTrue(copilotHttp(dir, IS_WINDOWS, hooksOf(dir)) { true } != null)
    }

    @Test
    fun `COPILOT_HOME moves the hooks file and an empty value falls back to the user home`() {
        val tail = File("hooks", "ide-agent-tabs.json").path
        assertEquals(File(File("c", "h"), tail), copilotHooksFile(File("c", "h").path, "u"))
        assertEquals(File(File("u", ".copilot"), tail), copilotHooksFile("", "u"))
        assertEquals(File(File("u", ".copilot"), tail), copilotHooksFile(null, "u"))
    }

    @Test
    fun `the health check needs this service on the port`() {
        val server = com.sun.net.httpserver.HttpServer.create(java.net.InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/health") { exchange ->
            val body = """{"service": "ide-agent-tabs"}""".toByteArray()
            exchange.sendResponseHeaders(200, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        val port = server.address.port
        try {
            assertTrue(answersHealth(port))
        } finally {
            server.stop(0)
        }
        assertFalse(answersHealth(port))
    }

    @Test
    fun `planLaunch gives a Copilot tab the config file and its folder, and leaves other agents alone`() {
        val copilot = BUILTIN_PROFILES.first { it.name == "copilot" }
        val http = CopilotHttp("C:/h/mcp/copilot-http.json", 47951)
        val folder = "C:\\work\\my repo\\100%"
        val launch = planLaunch(copilot, LaunchContext(copilot = http, cwd = folder, prompt = "hi", env = mapOf("X" to "1")))
        assertEquals(listOf("--additional-mcp-config", "@C:/h/mcp/copilot-http.json", "-i"), launch.args)
        assertEquals(mapOf("X" to "1", CWD_ENV to "C:\\work\\my repo\\100%25"), launch.env)
        launch.args.forEach { assertFalse(it, Regex("[\\s\"%]").containsMatchIn(it)) }
        assertEquals(emptyList<String>(), planLaunch(copilot, LaunchContext(cwd = folder)).args)
        assertEquals(emptyMap<String, String>(), planLaunch(copilot, LaunchContext(cwd = folder)).env)
        val codex = BUILTIN_PROFILES.first { it.name == "codex" }
        assertEquals(CODEX_TAB_ARGS, planLaunch(codex, LaunchContext(copilot = http, cwd = "/w")).args)
        listOf("copilot", "COPILOT.CMD", "C:\\npm\\copilot.ps1", "/usr/local/bin/copilot").forEach { assertTrue(it, isCopilotCommand(it)) }
        listOf("codex", "copilot-x", "notcopilot").forEach { assertFalse(it, isCopilotCommand(it)) }
        assertEquals(listOf("--additional-mcp-config", "@C:/h/mcp/copilot-http.json") to emptyMap<String, String>(), withCopilotHttp("copilot", emptyList(), emptyMap(), http, null))
    }

    @Test
    fun `the command patterns refuse a trailing newline`() {
        for (end in listOf("\n", "\r\n", "\r", "\u0085", " ")) {
            assertFalse(isCopilotCommand("copilot$end"))
            assertFalse(isCopilotCommand("copilot.exe$end"))
            assertEquals(listOf("--x") to emptyList<String>(), withClaudeSettings("claude$end", listOf("--x"), emptyList(), "/h/s.json"))
        }
        assertEquals(listOf("--x", "--settings", "/h/s.json") to emptyList<String>(), withClaudeSettings("claude", listOf("--x"), emptyList(), "/h/s.json"))
    }

    @Test
    fun `the folder header percent-encodes as the Python server copy does`() {
        assertEquals("C:\\work\\my repo\\100%25", cwdHeader("C:\\work\\my repo\\100%"))
        assertEquals("/tmp/Zo%C3%AB/%24%7BHOME%7D/a%26b~c", cwdHeader("/tmp/Zoë/\${HOME}/a&b~c"))
        assertEquals("/tmp/%E6%97%A5%E6%9C%AC", cwdHeader("/tmp/日本"))
    }

    @Test
    fun `a Claude Code tab gets the tab settings file after its profile arguments`() {
        val settingsFile = home.resolve("mcp").resolve(CLAUDE_TAB_SETTINGS_FILE)
        Files.createDirectories(home.resolve("mcp"))
        assertNull(claudeTabSettings(home, IS_WINDOWS))
        Files.writeString(settingsFile, "{}")
        assertNull(claudeTabSettings(home, IS_WINDOWS))
        val python = home.resolve("python.exe")
        Files.writeString(python, "")
        Files.writeString(home.resolve("mcp").resolve("python.json"), JsonParser.parseString("{}").asJsonObject.apply { addProperty("python", python.toString()) }.toString())
        assertEquals(settingsFile.toString(), claudeTabSettings(home, IS_WINDOWS))
        Files.delete(settingsFile)
        assertNull(claudeTabSettings(home, IS_WINDOWS))

        val claude = BUILTIN_PROFILES.first { it.name == "claude" }
        fun plan(profile: AgentProfile = claude, args: List<String> = emptyList(), settings: String? = "/h/s.json", model: String? = null, setting: LaunchVia = LaunchVia.DIRECT) =
            planLaunch(profile, LaunchContext(prompt = "hi", args = args, model = model, setting = setting, ori = DetectedOri("/bin/ori", null, listOf("claude")), claudeSettings = settings))
        assertEquals(listOf("--settings", "/h/s.json", "--model", "opus", "--resume", "abc"), plan(args = listOf("--resume", "abc"), model = "opus").args)
        val bypass = claude.copy(args = listOf("--permission-mode", "bypassPermissions"))
        assertEquals(listOf("--permission-mode", "bypassPermissions", "--settings", "/h/s.json"), plan(bypass).args)
        for (command in listOf("/usr/local/bin/claude", "C:\\Users\\a b\\.local\\bin\\claude.exe", "CLAUDE.CMD")) {
            assertEquals(command, listOf("--settings", "/h/s.json"), plan(claude.copy(command = command)).args)
        }
        val ori = plan(setting = LaunchVia.ORI)
        assertEquals(LaunchVia.ORI, ori.via)
        assertEquals(listOf("claude", "--settings", "/h/s.json"), ori.args)
        for (name in listOf("copilot", "pi")) {
            assertFalse(name, "--settings" in plan(BUILTIN_PROFILES.first { it.name == name }).args)
        }
        assertEquals(emptyList<String>(), plan(claude.copy(command = "claude-wrapper")).args)
        assertEquals(listOf("--settings=/missing/x.json"), plan(claude.copy(args = listOf("--settings=/missing/x.json"))).args)
        assertEquals(listOf("--settings", "{not json"), plan(args = listOf("--settings", "{not json")).args)
        assertEquals(emptyList<String>(), plan(settings = null).args)
    }

    @Test
    fun `a Claude Code tab that brings its own settings gets one file that merges them with the tab hooks`() {
        val ours = """{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"/py","args":["hook","claude","Stop"]}]}]}}"""
        val settingsFile = home.resolve("mcp").resolve(CLAUDE_TAB_SETTINGS_FILE)
        Files.createDirectories(settingsFile.parent)
        Files.writeString(settingsFile, ours)
        val user = """{"permissions":{"allow":["Bash(git *)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"mine"}]}]}}"""
        Files.writeString(home.resolve("mine.json"), user)
        fun merged(args: List<String>): Pair<List<String>, com.google.gson.JsonObject> {
            val i = args.indexOf("--settings")
            return (args.take(i) + args.drop(i + 2)) to JsonParser.parseString(Files.readString(Path.of(args[i + 1]))).asJsonObject
        }
        val claude = BUILTIN_PROFILES.first { it.name == "claude" }
        val override = claude.copy(args = listOf("--permission-mode", "bypassPermissions", "--settings", home.resolve("mine.json").toString()))
        val context = LaunchContext(model = "opus", claudeSettings = settingsFile.toString())
        val args = planLaunch(override, context).args
        assertEquals(1, args.count { it == "--settings" })
        val (rest, file) = merged(args)
        assertEquals(listOf("--permission-mode", "bypassPermissions", "--model", "opus"), rest)
        assertEquals(JsonParser.parseString(user).asJsonObject.get("permissions"), file.get("permissions"))
        assertEquals(listOf("mine", "/py"), file.getAsJsonObject("hooks").getAsJsonArray("Stop").map { it.asJsonObject.getAsJsonArray("hooks")[0].asJsonObject.get("command").asString })
        assertEquals(args, planLaunch(override, context).args)
        val relative = merged(planLaunch(claude.copy(args = listOf("--settings=mine.json")), LaunchContext(claudeSettings = settingsFile.toString(), cwd = home.toString())).args)
        assertEquals(emptyList<String>(), relative.first)
        assertEquals(listOf("--settings=mine.json"), planLaunch(claude.copy(args = listOf("--settings=mine.json")), LaunchContext(claudeSettings = settingsFile.toString())).args)
        val inline = merged(
            planLaunch(
                claude.copy(args = listOf("--settings", """{"model":"profile"}""")),
                LaunchContext(args = listOf("--settings", """{"model":"request"}"""), claudeSettings = settingsFile.toString()),
            ).args,
        )
        assertEquals(emptyList<String>(), inline.first)
        assertEquals("request", inline.second.get("model").asString)
        for (value in listOf("""{"hooks": []}""", """{"hooks": {"Stop": {}}}""", "[]")) {
            assertEquals(value, listOf("--settings", value), planLaunch(claude, LaunchContext(args = listOf("--settings", value), claudeSettings = settingsFile.toString())).args)
        }
    }
}
