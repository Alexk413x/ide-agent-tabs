package dev.alexk.claudestudiotabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ClaudeCommandTest {

    private val pwsh: Path? = findOnPath(System.getenv("PATH"), "pwsh.exe")?.let { Path.of(it) }

    @Test
    fun `finds an executable on PATH and skips blank, quoted and invalid entries`() {
        val dir = Files.createTempDirectory("cst-path")
        Files.createFile(dir.resolve("tool.exe"))
        val path = listOf("", "  ", "C:\\no\\such\\dir", "bad<>|dir", "\"$dir\"").joinToString(";")
        assertEquals(dir.resolve("tool.exe").toString(), findOnPath(path, "tool.exe"))
        assertEquals(null, findOnPath(path, "absent.exe"))
    }

    @Test
    fun `shell gets no arguments so the terminal's integration arguments stay intact`() {
        val launch = claudeLaunch("pwsh.exe", prompt = null)
        assertEquals(listOf("pwsh.exe"), launch.command)
        assertEquals(mapOf(STARTUP_ENV to "claude"), launch.env)
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

    private fun runThroughIntegration(prompt: String?): String {
        assumeTrue("pwsh.exe not on PATH", pwsh != null)
        val integration = javaClass.classLoader.getResource("shell-integrations/powershell/powershell-integration.ps1")
        assertNotNull("terminal plugin's PowerShell integration script is not on the test classpath", integration)
        val dir = Files.createTempDirectory("cst-integration")
        val script = dir.resolve("powershell-integration.ps1")
        integration!!.openStream().use { Files.copy(it, script) }

        val echo = dir.resolve("echo.ps1")
        Files.writeString(echo, """
            ${'$'}text = "${'$'}(${'$'}args.Count)|${'$'}(${'$'}args[0])|${'$'}(${'$'}env:$PROMPT_ENV ?? 'null')"
            ${'$'}bytes = [Text.Encoding]::UTF8.GetBytes(${'$'}text)
            [Console]::OpenStandardOutput().Write(${'$'}bytes, 0, ${'$'}bytes.Length)
        """.trimIndent(), Charsets.UTF_8)

        val launch = claudeLaunch(pwsh.toString(), prompt, claude = "& '$pwsh' -NoProfile -File '$echo'")
        val command = launch.command + listOf("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script.toString())
        val process = ProcessBuilder(command).apply { environment().putAll(launch.env) }.start()
        process.waitFor(60, TimeUnit.SECONDS)
        return process.inputStream.readAllBytes().toString(Charsets.UTF_8)
    }
}
