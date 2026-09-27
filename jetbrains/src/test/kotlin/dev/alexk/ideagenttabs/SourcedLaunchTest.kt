package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.util.concurrent.TimeUnit

class SourcedLaunchTest {

    private val dir: Path = Files.createTempDirectory("cst-sourced")
    private val isWindows = File.separatorChar == '\\'

    @Test
    fun `shell kind comes from the file name`() {
        assertEquals(ShellKind.POSIX, shellKind("/bin/zsh"))
        assertEquals(ShellKind.POSIX, shellKind("/usr/local/bin/bash"))
        assertEquals(ShellKind.FISH, shellKind("/opt/homebrew/bin/fish"))
        assertEquals(ShellKind.POWERSHELL, shellKind("/usr/local/bin/pwsh"))
        assertEquals(ShellKind.POWERSHELL, shellKind("pwsh.exe"))
        assertNull(shellKind("/bin/tcsh"))
    }

    @Test
    fun `unix shell uses the login shell and falls back to bash`() {
        assertEquals("/bin/zsh", unixShell("/bin/zsh", isMac = true).path)
        assertEquals(ShellKind.FISH, unixShell("/usr/bin/fish", isMac = false).kind)
        for (other in listOf(null, "", "/bin/tcsh", "/bin/sh")) {
            val shell = unixShell(other, isMac = false)
            assertEquals(other, "/bin/bash", shell.path)
            assertEquals(other, ShellKind.POSIX, shell.kind)
        }
    }

    @Test
    fun `unix shells start interactive, and as login shells on macOS`() {
        assertEquals(listOf("-l", "-i"), unixShell("/bin/zsh", isMac = true).flags)
        assertEquals(listOf("-i"), unixShell("/bin/bash", isMac = false).flags)
        assertEquals(listOf("-l", "-i"), unixShell("/bin/tcsh", isMac = true).flags)
        assertEquals(emptyList<String>(), unixShell("/usr/local/bin/pwsh", isMac = true).flags)
    }

    private val claude = AgentProfile("claude", "Claude Code", "claude")

    @Test
    fun `sourced launch points the shell at the script and passes the command and each arg in its own variable`() {
        val script = dir.resolve("agent.sh")
        val gemini = AgentProfile("gemini", "Gemini CLI", "gemini", promptFlag = "-i")
        val launch = sourcedLaunch(unixShell("/bin/zsh", isMac = true), script, gemini.launch("hi", listOf("--a", ""), mapOf("FOO" to "bar")), "tab-1")
        assertEquals(listOf("/bin/zsh", "-l", "-i"), launch.command)
        assertEquals(
            mapOf(
                STARTUP_ENV to script.toString(),
                TAB_ID_ENV to "tab-1",
                AGENT_ENV to "gemini",
                COMMAND_ENV to "gemini",
                PROMPT_ENV to "hi",
                ARG_COUNT_ENV to "3",
                "${ARG_ENV_PREFIX}0" to "--a",
                "${ARG_ENV_PREFIX}1" to "",
                "${ARG_ENV_PREFIX}2" to "-i",
                "FOO" to "bar",
            ),
            launch.env,
        )
        assertEquals(
            mapOf(STARTUP_ENV to script.toString(), TAB_ID_ENV to "tab-2", AGENT_ENV to "claude", COMMAND_ENV to "claude"),
            sourcedLaunch(unixShell("/bin/bash", false), script, claude.launch(null), "tab-2").env,
        )
    }

    @Test
    fun `launch script is written once and rewritten when it differs`() {
        val script = launchScript(ShellKind.POSIX, dir.resolve("scripts"))
        assertEquals("agent.sh", script.fileName.toString())
        val written = Files.getLastModifiedTime(script)
        Thread.sleep(20)
        launchScript(ShellKind.POSIX, dir.resolve("scripts"))
        assertEquals(written, Files.getLastModifiedTime(script))
        Files.writeString(script, "stale")
        launchScript(ShellKind.POSIX, dir.resolve("scripts"))
        assertTrue(Files.readString(script).contains("__ide_agent_tabs"))
        assertEquals("agent.fish", launchScript(ShellKind.FISH, dir.resolve("scripts")).fileName.toString())
        assertFalse(Files.readString(script).contains('\r'))
    }

    @Test
    fun `plugin variables and the startup variables are reserved`() {
        for (name in listOf(STARTUP_ENV, "jediterm_source_args", TAB_ID_ENV, "${ARG_ENV_PREFIX}0", "ide_agent_tabs_argc")) {
            assertTrue(name, isReservedEnv(name))
        }
        assertFalse(isReservedEnv("CLAUDE_CODE_USE_BEDROCK"))
    }

    @Test
    fun `bash runs the agent through the real integration script`() = checkShell("bash")

    @Test
    fun `zsh runs the agent through the real integration script`() = checkShell("zsh")

    @Test
    fun `fish runs the agent through the real integration script`() = checkShell("fish")

    private fun checkShell(name: String) {
        assumeTrue("POSIX shells are tested on macOS and Linux", !isWindows)
        val shell = findOnPath(System.getenv("PATH").orEmpty(), name)
        assumeTrue("$name not on PATH", shell != null)
        val agent = AgentProfile("test", "Test", "cst-agent")
        val spaced = AgentProfile("spaced", "Spaced", dir.resolve("my bin/cst agent").toString(), listOf("--model", "m b"), promptFlag = "-i")
        for (isMac in listOf(false, true)) {
            val unix = unixShell(shell!!, isMac)
            assertEquals("0||unset|test|", run(unix, agent.launch(null)))
            val prompt = """Say "hi" & run $(whoami); `tick` 'quote' --flag é ✓ 🙂
second line"""
            assertEquals("1|$prompt\u001f|unset|test|", run(unix, agent.launch(prompt)))
            val args = listOf("--plugin-dir", "/a b/c", """say "hi" $(whoami) `t` *""", "", "é ✓")
            val out = run(unix, agent.launch("the prompt", args, mapOf("CST_TEST_VAR" to "value with spaces")))
            assertEquals("6|" + (args + "the prompt").joinToString("") { "$it\u001f" } + "|unset|test|value with spaces", out)
            assertEquals("4|--model\u001fm b\u001f-i\u001fhi\u001f|unset|spaced|", run(unix, spaced.launch("hi")))
        }
    }

    private fun run(shell: Shell, agent: AgentLaunch): String {
        val out = dir.resolve("out")
        Files.deleteIfExists(out)
        val fake = """
            #!/bin/sh
            out=""; for x in "$@"; do out="${'$'}out${'$'}x$(printf '\037')"; done
            printf '%s|%s|%s|%s|%s' "$#" "${'$'}out" "${'$'}{$PROMPT_ENV-${'$'}{${ARG_ENV_PREFIX}0-${'$'}{$ARG_COUNT_ENV-${'$'}{$COMMAND_ENV-unset}}}}" "${'$'}$AGENT_ENV" "${'$'}CST_TEST_VAR" > "${'$'}CST_OUT"
        """.trimIndent() + "\n"
        val bin = Files.createDirectories(dir.resolve("bin"))
        for (target in listOf(bin.resolve("cst-agent"), Files.createDirectories(dir.resolve("my bin")).resolve("cst agent"))) {
            Files.writeString(target, fake)
            Files.setPosixFilePermissions(target, PosixFilePermissions.fromString("rwxr-xr-x"))
        }

        val integration = copyIntegrations()
        val launch = sourcedLaunch(shell, launchScript(shell.kind, dir.resolve("scripts")), agent, "tab-1")
        // Mirrors LocalShellIntegrationInjector: bash gets --rcfile first and trades -l for LOGIN_SHELL,
        // zsh gets ZDOTDIR, fish gets --init-command.
        val injectedEnv = mutableMapOf<String, String>()
        val command = launch.command.toMutableList()
        when {
            shell.path.endsWith("bash") -> {
                if (command.removeAll(listOf("-l", "--login"))) injectedEnv["LOGIN_SHELL"] = "1"
                command.addAll(1, listOf("--rcfile", "$integration/bash/bash-integration.bash"))
            }
            shell.path.endsWith("zsh") -> {
                injectedEnv["ZDOTDIR"] = "$integration/zsh/zdotdir"
                injectedEnv["JETBRAINS_INTELLIJ_ZSH_DIR"] = "$integration/zsh"
            }
            else -> command += "--init-command=source $integration/fish/fish-integration.fish"
        }
        val process = ProcessBuilder(command).apply {
            environment().putAll(launch.env)
            environment().putAll(injectedEnv)
            environment()["PATH"] = "$bin${File.pathSeparator}${System.getenv("PATH")}"
            environment()["CST_OUT"] = out.toString()
            redirectInput(ProcessBuilder.Redirect.from(File("/dev/null")))
            redirectOutput(ProcessBuilder.Redirect.DISCARD)
            redirectError(ProcessBuilder.Redirect.DISCARD)
        }.start()
        assertTrue("$shell did not exit", process.waitFor(60, TimeUnit.SECONDS))
        return if (Files.exists(out)) Files.readString(out) else "the agent did not run"
    }

    private fun copyIntegrations(): Path {
        val root = dir.resolve("shell-integrations")
        val files = listOf(
            "bash/bash-integration.bash", "bash/bash-preexec.bash", "bash/bash-fig.bash",
            "bash/command-block-support.bash", "bash/command-block-support-reworked.bash",
            "zsh/zsh-integration.zsh", "zsh/command-block-support.zsh", "zsh/command-block-support-reworked.zsh",
            "zsh/zdotdir/.zshenv", "zsh/zdotdir/.zprofile", "zsh/zdotdir/.zshrc", "zsh/zdotdir/.zlogin",
            "zsh/zdotdir/source-original.zsh",
            "fish/fish-integration.fish", "fish/command-block-support.fish", "fish/command-block-support-reworked.fish",
        )
        for (file in files) {
            val target = root.resolve(file)
            if (Files.exists(target)) continue
            val resource = javaClass.classLoader.getResourceAsStream("shell-integrations/$file") ?: continue
            Files.createDirectories(target.parent)
            resource.use { Files.copy(it, target) }
        }
        return root
    }
}
