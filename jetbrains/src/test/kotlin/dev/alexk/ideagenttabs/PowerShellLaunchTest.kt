package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class PowerShellLaunchTest {

    private val pwsh: Path? = findOnPath(System.getenv("PATH"), if (File.separatorChar == '\\') "pwsh.exe" else "pwsh")?.let { Path.of(it) }

    @Test
    fun `finds an executable on PATH and skips blank, quoted and invalid entries`() {
        val dir = Files.createTempDirectory("cst-path")
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

    private val claude = AgentProfile("claude", "Claude Code", "claude")

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

    private fun runThroughIntegration(
        prompt: String?,
        args: List<String> = emptyList(),
        env: Map<String, String> = emptyMap(),
        listArgs: Boolean = false,
        promptFlag: String? = null,
        shell: Path? = pwsh,
    ): String {
        assumeTrue("pwsh not on PATH", pwsh != null)
        val integration = javaClass.classLoader.getResource("shell-integrations/powershell/powershell-integration.ps1")
        assertNotNull("terminal plugin's PowerShell integration script is not on the test classpath", integration)
        val dir = Files.createTempDirectory("cst-integration")
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

        val profile = AgentProfile("test", "Test", pwsh.toString(), listOf("-NoProfile", "-File", echo.toString()), promptFlag)
        val launch = powerShellLaunch(shell.toString(), launchScript(ShellKind.POWERSHELL, dir), profile.launch(prompt, args, env), tabId = "tab-3")
        val command = launch.command + listOf("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script.toString())
        val process = ProcessBuilder(command).apply { environment().putAll(launch.env) }.start()
        process.waitFor(60, TimeUnit.SECONDS)
        return process.inputStream.readAllBytes().toString(Charsets.UTF_8)
    }
}
