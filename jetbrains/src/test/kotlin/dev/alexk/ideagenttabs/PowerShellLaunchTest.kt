package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class PowerShellLaunchTest {

    @get:Rule
    val temp = TempDirs()

    private val pwsh: Path? = findOnPath(System.getenv("PATH"), if (IS_WINDOWS) "pwsh.exe" else "pwsh")?.let { Path.of(it) }

    @Test
    fun `finds an executable on PATH and skips blank, quoted and invalid entries`() {
        val dir = temp.create("cst-path")
        Files.createFile(dir.resolve("tool.exe"))
        val path = listOf("", "  ", "C:\\no\\such\\dir", "bad<>|dir", "\"$dir\"").joinToString(File.pathSeparator)
        assertEquals(dir.resolve("tool.exe").toString(), findOnPath(path, "tool.exe"))
        assertEquals(null, findOnPath(path, "absent.exe"))
    }

    @Test
    fun `finds the Microsoft Store pwsh alias`() {
        val windowsApps = Path.of(System.getenv("LOCALAPPDATA").orEmpty(), "Microsoft", "WindowsApps")
        assumeTrue("Store pwsh not installed", Files.exists(windowsApps.resolve("pwsh.exe"), LinkOption.NOFOLLOW_LINKS))
        assertEquals(windowsApps.resolve("pwsh.exe").toString(), findOnPath(windowsApps.toString(), "pwsh.exe"))
    }

    private val script = Path.of("launch", "agent.ps1")

    @Test
    fun `shell gets no arguments so the terminal's integration arguments stay intact`() {
        val launch = powerShellLaunch("pwsh.exe", script, claude.launch(null), tabId = "tab-1")
        assertEquals(listOf("pwsh.exe"), launch.command)
        assertEquals(
            mapOf(
                STARTUP_ENV to script.toString(),
                COMMAND_ENV to "claude",
                AGENT_ENV to "claude",
                TAB_ID_ENV to "tab-1",
            ),
            launch.env,
        )
    }

    @Test
    fun `caller text travels only in its own variables`() {
        val profile = AgentProfile("x", "X", "C:\\a b\\x;y.exe", args = listOf("\$(whoami)"), promptFlag = "-p")
        val launch = powerShellLaunch("pwsh.exe", script, profile.launch("'; Remove-Item C:\\ #", listOf("`t")), tabId = "tab-9")
        assertEquals(script.toString(), launch.env[STARTUP_ENV])
        assertEquals("""["$(whoami)","`t","-p"]""", launch.env[ARGS_ENV])
        assertEquals("'; Remove-Item C:\\ #", launch.env[PROMPT_ENV])
        assertEquals("C:\\a b\\x;y.exe", launch.env[COMMAND_ENV])
    }

    @Test
    fun `tabs get the Agent Tabs home only when the IDE was pointed at another one`() {
        assertEquals(null, powerShellLaunch("pwsh.exe", script, claude.launch(null), tabId = "t").env[HOME_ENV])
        assertEquals("D:\\iat home", powerShellLaunch("pwsh.exe", script, claude.launch(null), tabId = "t", home = "D:\\iat home").env[HOME_ENV])
        val sourced = sourcedLaunch(Shell("/bin/bash", ShellKind.POSIX), script, claude.launch(null), tabId = "t", home = "/x/iat")
        assertEquals("/x/iat", sourced.env[HOME_ENV])
    }

    @Test
    fun `reserved variables the IDE inherited are emptied unless the launch sets them`() {
        val inherited = mapOf(
            "PATH" to "/bin",
            PROMPT_ENV to "old prompt",
            ARGS_ENV to "[\"old\"]",
            "ide_agent_tabs_argc" to "3",
            "IDE_AGENT_TABS_ARG_0" to "old",
            TAB_ID_ENV to "parent",
            HOME_ENV to "/kept",
            "JEDITERM_SOURCE_ARGS" to "x",
        )
        for (launch in listOf(
            powerShellLaunch("pwsh.exe", script, claude.launch(null), tabId = "tab-7", inherited = inherited),
            sourcedLaunch(Shell("/bin/bash", ShellKind.POSIX), script, claude.launch(null), tabId = "tab-7", inherited = inherited),
        )) {
            assertEquals("", launch.env[PROMPT_ENV])
            assertEquals("", launch.env[ARGS_ENV])
            assertEquals("", launch.env["ide_agent_tabs_argc"])
            assertEquals("", launch.env["IDE_AGENT_TABS_ARG_0"])
            assertEquals("", launch.env["JEDITERM_SOURCE_ARGS"])
            assertEquals("tab-7", launch.env[TAB_ID_ENV])
            assertEquals(script.toString(), launch.env[STARTUP_ENV])
            assertEquals(null, launch.env["PATH"])
            assertEquals(null, launch.env[HOME_ENV])
        }
        val withPrompt = powerShellLaunch("pwsh.exe", script, claude.launch("new"), tabId = "t", inherited = inherited)
        assertEquals("new", withPrompt.env[PROMPT_ENV])
    }

    @Test
    fun `an emptied prompt and args reach no agent`() {
        assertEquals("0||null|test", runThroughIntegration(prompt = null, env = emptyMap(), inheritedReserved = true))
    }

    @Test
    fun `every launch carries its tab id so the session can close its own tab`() {
        assertEquals("tab-2", powerShellLaunch("pwsh.exe", script, claude.launch("hi"), tabId = "tab-2").env[TAB_ID_ENV])
    }

    @Test
    fun `caller env reaches the shell and the plugin's own variables win`() {
        val launch = powerShellLaunch("pwsh.exe", script, claude.launch(null, callerEnv = mapOf("FOO" to "bar")), tabId = "tab-4")
        assertEquals("bar", launch.env["FOO"])
        assertEquals("tab-4", launch.env[TAB_ID_ENV])
        assertEquals(null, launch.env[ARGS_ENV])
    }

    @Test
    fun `plain launch runs the agent through the real integration script`() {
        assertEquals("0||null|test", runThroughIntegration(prompt = null))
    }

    @Test
    fun `prompt reaches a native program as one intact argument through the real integration script`() {
        val prompt = """Say "hi" & run $(whoami); `tick` 'quote' --flag é ✓ 🙂
second line"""
        assertEquals("1|$prompt|null|test", runThroughIntegration(prompt))
    }

    @Test
    fun `args reach a native program intact and before the prompt, and env reaches the session`() {
        val args = listOf("--plugin-dir", "C:\\Program Files\\a b", """say "hi" $(whoami) `t` 'q'""", "é ✓")
        val out = runThroughIntegration("the prompt", args, mapOf("IAT_TEST_VAR" to "value with spaces"), listArgs = true)
        val expected = (args + "the prompt").joinToString("\u001f") + "|value with spaces|null|test"
        assertEquals(expected, out)
    }

    @Test
    fun `the prompt flag goes right before the prompt`() {
        val out = runThroughIntegration("the prompt", listOf("--yolo"), listArgs = true, promptFlag = "--prompt")
        assertEquals(listOf("--yolo", "--prompt", "the prompt").joinToString("\u001f") + "||null|test", out)
    }

    @Test
    fun `a date-like arg keeps its exact text`() {
        val out = runThroughIntegration(prompt = null, args = listOf("2024-01-01T00:00:00Z"), listArgs = true)
        assertEquals("2024-01-01T00:00:00Z||null|test", out)
    }

    @Test
    fun `Windows PowerShell keeps embedded quotes and empty args on the way to a native program`() {
        val windowsPowerShell = findOnPath(System.getenv("PATH").orEmpty(), "powershell.exe")?.let { Path.of(it) }
        assumeTrue("Windows PowerShell not on PATH", windowsPowerShell != null)
        val args = listOf("say \"hi\"", "", "C:\\dir with space\\", "plain")
        val out = runThroughIntegration("a \"quoted\" prompt", args, listArgs = true, shell = windowsPowerShell)
        assertEquals((args + "a \"quoted\" prompt").joinToString("") + "||null|test", out)
    }

    @Test
    fun `a single arg stays one argument`() {
        val out = runThroughIntegration(prompt = null, args = listOf("--verbose"), listArgs = true)
        assertEquals("--verbose||null|test", out)
    }

    @Test
    fun `a batch agent gets hostile arguments intact and nothing else runs`() {
        assumeTrue("batch files run on Windows only", IS_WINDOWS)
        val windowsPowerShell = findOnPath(System.getenv("PATH").orEmpty(), "powershell.exe")?.let { Path.of(it) }
        // A line break cannot cross cmd.exe, so the launcher turns it into a space.
        val prompt = "he said \"hi & echo x>canary-prompt & rem \nsecond line"
        val args = listOf(
            "a&echo x>canary-amp", "x|y", "\"quoted & amp\"", "100%PATH%", "%", "^caret", "a<b>c", "trailing\\", "", "q\\\"uote & echo x>canary-quote", "!x!", "(a),b;c=d",
        )
        for (shell in listOfNotNull(pwsh, windowsPowerShell)) {
            val expected = (args + "--prompt" + prompt.replace("\n", " ")).joinToString("\u001f") + "||null|test"
            assertEquals(shell.toString(), expected, runThroughIntegration(prompt, args, listArgs = true, promptFlag = "--prompt", shell = shell, viaBatch = true))
        }
    }

    private fun runThroughIntegration(
        prompt: String?,
        args: List<String> = emptyList(),
        env: Map<String, String> = emptyMap(),
        listArgs: Boolean = false,
        promptFlag: String? = null,
        shell: Path? = pwsh,
        viaBatch: Boolean = false,
        inheritedReserved: Boolean = false,
    ): String {
        assumeTrue("pwsh not on PATH", pwsh != null)
        val integration = javaClass.classLoader.getResource("shell-integrations/powershell/powershell-integration.ps1")
        assertNotNull("terminal plugin's PowerShell integration script is not on the test classpath", integration)
        val dir = temp.create("cst-integration")
        val script = dir.resolve("powershell-integration.ps1")
        integration!!.openStream().use { Files.copy(it, script) }

        val echo = dir.resolve("echo.ps1")
        val head = if (listArgs) {
            "${'$'}(${'$'}args -join [char]0x1f)|${'$'}(${'$'}env:IAT_TEST_VAR)"
        } else {
            "${'$'}(${'$'}args.Count)|${'$'}(${'$'}args[0])"
        }
        val leftover = "${'$'}env:$PROMPT_ENV ?? ${'$'}env:$ARGS_ENV ?? ${'$'}env:$COMMAND_ENV ?? 'null'"
        Files.writeString(echo, """
            ${'$'}text = "$head|${'$'}($leftover)|${'$'}(${'$'}env:$AGENT_ENV)"
            ${'$'}bytes = [Text.Encoding]::UTF8.GetBytes(${'$'}text)
            [Console]::OpenStandardOutput().Write(${'$'}bytes, 0, ${'$'}bytes.Length)
        """.trimIndent(), Charsets.UTF_8)

        val batch = dir.resolve("fake agent.cmd")
        Files.writeString(batch, "@echo off\r\n\"$pwsh\" -NoProfile -File \"$echo\" %*\r\n")
        val profile = if (viaBatch) {
            AgentProfile("test", "Test", batch.toString(), emptyList(), promptFlag)
        } else {
            AgentProfile("test", "Test", pwsh.toString(), listOf("-NoProfile", "-File", echo.toString()), promptFlag)
        }
        val inherited = if (inheritedReserved) mapOf(PROMPT_ENV to "old", ARGS_ENV to "[\"old\"]") else emptyMap()
        val launch = powerShellLaunch(shell.toString(), launchScript(ShellKind.POWERSHELL, dir), profile.launch(prompt, args, env), tabId = "tab-3", inherited = inherited)
        val command = launch.command + listOf("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script.toString())
        val process = ProcessBuilder(command).directory(dir.toFile()).apply { environment().putAll(launch.env) }.start()
        process.waitFor(60, TimeUnit.SECONDS)
        val out = process.inputStream.readAllBytes().toString(Charsets.UTF_8)
        assertEquals("no argument ran as a command", emptyList<String>(), dir.toFile().list().orEmpty().filter { it.startsWith("canary") })
        return out
    }
}
