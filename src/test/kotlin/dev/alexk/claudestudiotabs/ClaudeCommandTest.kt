package dev.alexk.claudestudiotabs

import org.junit.Assert.assertEquals
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ClaudeCommandTest {

    @Test
    fun `plain command runs claude and keeps the shell`() {
        assertEquals(listOf("pwsh.exe", "-NoLogo", "-NoExit", "-Command", "claude"), claudeCommand("pwsh.exe", withPrompt = false))
    }

    @Test
    fun `prompt reaches a native program as one intact argument`() {
        val pwsh = System.getenv("PATH").split(';').map { Path.of(it, "pwsh.exe") }.firstOrNull { Files.exists(it) }
        assumeTrue("pwsh.exe not on PATH", pwsh != null)
        val echo = Files.createTempFile("echo", ".ps1")
        Files.writeString(echo, """
            ${'$'}text = "${'$'}(${'$'}args.Count)|${'$'}(${'$'}args[0])|${'$'}(${'$'}env:$PROMPT_ENV ?? 'null')"
            ${'$'}bytes = [Text.Encoding]::UTF8.GetBytes(${'$'}text)
            [Console]::OpenStandardOutput().Write(${'$'}bytes, 0, ${'$'}bytes.Length)
        """.trimIndent(), Charsets.UTF_8)
        val prompt = """Say "hi" & run $(whoami); `tick` 'quote' --flag é ✓ 🙂
second line"""

        val command = claudeCommand(pwsh.toString(), withPrompt = true, claude = "& '$pwsh' -NoProfile -File '$echo'")
            .filter { it != "-NoExit" }
        val process = ProcessBuilder(command).apply { environment()[PROMPT_ENV] = prompt }.start()
        process.waitFor(60, TimeUnit.SECONDS)
        val out = process.inputStream.readAllBytes().toString(Charsets.UTF_8)

        assertEquals("1|$prompt|null", out)
    }
}
