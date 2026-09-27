package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path

class OpenOnStartupTest {

    private val plain: Path = Files.createTempDirectory("cst-plain")
    private val withClaude: Path = Files.createTempDirectory("cst-claude").also { Files.createDirectory(it.resolve(".claude")) }

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
    fun `a project without a folder never opens`() {
        for (mode in OpenOnStartup.entries) assertFalse(mode.name, opensOnStartup(mode, null))
    }
}
