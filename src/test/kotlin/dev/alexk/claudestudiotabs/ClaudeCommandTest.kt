package dev.alexk.claudestudiotabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ClaudeCommandTest {

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

    @Test
    fun `shell gets no arguments so the terminal's integration arguments stay intact`() {
        val launch = powerShellLaunch("pwsh.exe", prompt = null, tabId = "tab-1")
        assertEquals(listOf("pwsh.exe"), launch.command)
        assertEquals(mapOf(STARTUP_ENV to "claude", TAB_ID_ENV to "tab-1"), launch.env)
    }

    @Test
    fun `every launch carries its tab id so the session can close its own tab`() {
        assertEquals("tab-2", powerShellLaunch("pwsh.exe", prompt = "hi", tabId = "tab-2").env[TAB_ID_ENV])
    }

    @Test
    fun `caller env reaches the shell and the plugin's own variables win`() {
        val launch = powerShellLaunch("pwsh.exe", prompt = null, tabId = "tab-4", env = mapOf("FOO" to "bar"))
        assertEquals("bar", launch.env["FOO"])
        assertEquals("tab-4", launch.env[TAB_ID_ENV])
        assertEquals(null, launch.env[ARGS_ENV])
    }

    @Test
    fun `plain launch runs claude through the real integration script`() {
        assertEquals("0||null", runThroughIntegration(prompt = null))
    }

    @Test
    fun `prompt reaches a native program as one intact argument through the real integration script`() {
        val prompt = """Say "hi" & run $(whoami); `tick` 'quote' --flag é ✓ 🙂
second line"""
        assertEquals("1|$prompt|null", runThroughIntegration(prompt))
    }

    @Test
    fun `args reach a native program intact and before the prompt, and env reaches the session`() {
        val args = listOf("--plugin-dir", "C:\\Program Files\\a b", """say "hi" $(whoami) `t` 'q'""", "é ✓")
        val out = runThroughIntegration("the prompt", args, mapOf("CST_TEST_VAR" to "value with spaces"), listArgs = true)
        val expected = (args + "the prompt").joinToString("\u001f") + "|value with spaces|null"
        assertEquals(expected, out)
    }

    @Test
    fun `a single arg stays one argument`() {
        val out = runThroughIntegration(prompt = null, args = listOf("--verbose"), listArgs = true)
        assertEquals("--verbose||null", out)
    }

    private fun runThroughIntegration(
        prompt: String?,
        args: List<String> = emptyList(),
        env: Map<String, String> = emptyMap(),
        listArgs: Boolean = false,
    ): String {
        assumeTrue("pwsh not on PATH", pwsh != null)
        val integration = javaClass.classLoader.getResource("shell-integrations/powershell/powershell-integration.ps1")
        assertNotNull("terminal plugin's PowerShell integration script is not on the test classpath", integration)
        val dir = Files.createTempDirectory("cst-integration")
        val script = dir.resolve("powershell-integration.ps1")
        integration!!.openStream().use { Files.copy(it, script) }

        val echo = dir.resolve("echo.ps1")
        val head = if (listArgs) {
            "${'$'}(${'$'}args -join [char]0x1f)|${'$'}(${'$'}env:CST_TEST_VAR)"
        } else {
            "${'$'}(${'$'}args.Count)|${'$'}(${'$'}args[0])"
        }
        Files.writeString(echo, """
            ${'$'}text = "$head|${'$'}(${'$'}env:$PROMPT_ENV ?? ${'$'}env:$ARGS_ENV ?? 'null')"
            ${'$'}bytes = [Text.Encoding]::UTF8.GetBytes(${'$'}text)
            [Console]::OpenStandardOutput().Write(${'$'}bytes, 0, ${'$'}bytes.Length)
        """.trimIndent(), Charsets.UTF_8)

        val launch = powerShellLaunch(pwsh.toString(), prompt, tabId = "tab-3", claude = "& '$pwsh' -NoProfile -File '$echo'", args = args, env = env)
        val command = launch.command + listOf("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script.toString())
        val process = ProcessBuilder(command).apply { environment().putAll(launch.env) }.start()
        process.waitFor(60, TimeUnit.SECONDS)
        return process.inputStream.readAllBytes().toString(Charsets.UTF_8)
    }
}
