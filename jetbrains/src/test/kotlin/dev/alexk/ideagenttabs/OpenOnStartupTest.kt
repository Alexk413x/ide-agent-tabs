package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path

class OpenOnStartupTest {

    @get:Rule
    val temp = TempDirs()

    private val plain: Path = temp.create("cst-plain")
    private val withClaude: Path = temp.create("cst-claude").also { Files.createDirectory(it.resolve(".claude")) }

    @Test
    fun `values match the VS Code setting`() {
        assertEquals(listOf("claudeFolder", "always", "never"), OpenOnStartup.entries.map { it.value })
        for (mode in OpenOnStartup.entries) assertEquals(mode, OpenOnStartup.of(mode.value))
    }

    @Test
    fun `a missing or unknown value means claudeFolder`() {
        assertEquals(OpenOnStartup.CLAUDE_FOLDER, OpenOnStartup.of(null))
        assertEquals(OpenOnStartup.CLAUDE_FOLDER, OpenOnStartup.of("Always"))
        assertEquals(OpenOnStartup.CLAUDE_FOLDER, OpenOnStartup.of(""))
    }

    @Test
    fun `claudeFolder opens only when the project has a claude folder`() {
        assertTrue(opensOnStartup(OpenOnStartup.CLAUDE_FOLDER, withClaude))
        assertFalse(opensOnStartup(OpenOnStartup.CLAUDE_FOLDER, plain))
    }

    @Test
    fun `a claude file is not a claude folder`() {
        Files.writeString(plain.resolve(".claude"), "")
        assertFalse(opensOnStartup(OpenOnStartup.CLAUDE_FOLDER, plain))
    }

    @Test
    fun `always opens and never does not`() {
        assertTrue(opensOnStartup(OpenOnStartup.ALWAYS, plain))
        assertTrue(opensOnStartup(OpenOnStartup.ALWAYS, withClaude))
        assertFalse(opensOnStartup(OpenOnStartup.NEVER, plain))
        assertFalse(opensOnStartup(OpenOnStartup.NEVER, withClaude))
    }

    @Test
    fun `an untrusted project waits for trust and a trusted one opens at once`() {
        assertEquals(Startup.OPEN, startupAction(OpenOnStartup.CLAUDE_FOLDER, withClaude, trusted = true))
        assertEquals(Startup.AFTER_TRUST, startupAction(OpenOnStartup.CLAUDE_FOLDER, withClaude, trusted = false))
        assertEquals(Startup.AFTER_TRUST, startupAction(OpenOnStartup.ALWAYS, plain, trusted = false))
        assertEquals(Startup.OPEN, startupAction(OpenOnStartup.ALWAYS, plain, trusted = true))
    }

    @Test
    fun `nothing opens or waits when the setting or the folder says no, trusted or not`() {
        for (trusted in listOf(true, false)) {
            assertEquals(Startup.NONE, startupAction(OpenOnStartup.NEVER, withClaude, trusted))
            assertEquals(Startup.NONE, startupAction(OpenOnStartup.CLAUDE_FOLDER, plain, trusted))
            assertEquals(Startup.NONE, startupAction(OpenOnStartup.ALWAYS, null, trusted))
        }
    }

    @Test
    fun `a project without a folder never opens`() {
        for (mode in OpenOnStartup.entries) assertFalse(mode.name, opensOnStartup(mode, null))
    }
}
