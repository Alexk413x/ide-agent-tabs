package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class AgentTabRegistryTest {

    @Test
    fun `a terminal opened as an agent tab comes back from its env and folder`() {
        val env = mapOf(TAB_ID_ENV to "tab-1", AGENT_ENV to "codex", COMMAND_ENV to "codex", "PATH" to "/bin")
        val tab = revivedTab(env, "/work/app")!!
        assertEquals(listOf("tab-1", "codex", "/work/app"), listOf(tab.id, tab.agent, tab.path))
    }

    @Test
    fun `a terminal without a tab id is not an agent tab`() {
        assertNull(revivedTab(mapOf("PATH" to "/bin"), "/work"))
        assertNull(revivedTab(mapOf(TAB_ID_ENV to ""), "/work"))
    }

    @Test
    fun `a tab id without an agent or folder still revives`() {
        val tab = revivedTab(mapOf(TAB_ID_ENV to "tab-2"), null)!!
        assertEquals(listOf("tab-2", "", ""), listOf(tab.id, tab.agent, tab.path))
    }
}
