package dev.alexk.ideagenttabs

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.FileTime

class SharedSettingsTest {

    private val home: Path = Files.createTempDirectory("cst-shared")
    private val warnings = mutableListOf<String>()
    private val settings = AgentSettings(home) { warnings += it }
    private val config = home.resolve(CONFIG_FILE)

    private var clock = System.currentTimeMillis()

    private fun write(name: String, text: String) {
        val file = home.resolve(name)
        Files.writeString(file, text)
        clock += 10_000
        Files.setLastModifiedTime(file, FileTime.fromMillis(clock))
    }

    private fun saved() = JsonParser.parseString(Files.readString(config)).asJsonObject

    @Test
    fun `defaults apply when config is missing or lacks the keys`() {
        assertEquals(SharedSettings(), settings.shared())
        write(CONFIG_FILE, """{"defaultAgent": "codex"}""")
        assertEquals(SharedSettings(TabRouting.PROJECT, AUTO, AUTO, TerminalWindow.LAST), settings.shared())
        assertTrue(warnings.isEmpty())
    }

    @Test
    fun `the four keys are read and invalid values fall back`() {
        write(CONFIG_FILE, """{"tabRouting": "caller", "terminal": "wezterm", "shell": "/opt/pwsh", "terminalWindow": "dedicated"}""")
        assertEquals(SharedSettings(TabRouting.CALLER, "wezterm", "/opt/pwsh", TerminalWindow.DEDICATED), settings.shared())
        write(CONFIG_FILE, """{"tabRouting": "nowhere", "terminal": 3, "shell": " ", "terminalWindow": ""}""")
        assertEquals(SharedSettings(), settings.shared())
        write(CONFIG_FILE, "broken")
        assertEquals(SharedSettings(), settings.shared())
        assertEquals(1, warnings.size)
    }

    @Test
    fun `saving a shared setting keeps unknown keys`() {
        assertTrue(settings.setTabRouting(TabRouting.CALLER))
        assertEquals("caller", saved().get("tabRouting").asString)

        Files.writeString(config, """{"defaultAgent": "codex", "jev": {"enabled": true, "tiers": {"codex": "x"}}, "terminal": "kitty"}""")
        assertTrue(settings.setTerminalWindow(TerminalWindow.DEDICATED))
        assertTrue(settings.setShell("/opt/microsoft/powershell/7/pwsh"))
        assertEquals(
            JsonParser.parseString("""{"defaultAgent": "codex", "jev": {"enabled": true, "tiers": {"codex": "x"}}, "terminal": "kitty", "terminalWindow": "dedicated", "shell": "/opt/microsoft/powershell/7/pwsh"}"""),
            saved(),
        )
        assertEquals(listOf(CONFIG_FILE), Files.list(home).use { s -> s.map { it.fileName.toString() }.toList() })
    }

    @Test
    fun `choosing Automatic removes the terminal and shell keys`() {
        Files.writeString(config, """{"terminal": "tmux", "shell": "/opt/pwsh", "other": 1}""")
        assertTrue(settings.setTerminal(AUTO))
        assertTrue(settings.setShell(""))
        assertEquals(JsonParser.parseString("""{"other": 1}"""), saved())
    }

    @Test
    fun `saving leaves a broken config alone`() {
        Files.writeString(config, "{broken")
        assertFalse(settings.setTerminal("wezterm"))
        assertEquals("{broken", Files.readString(config))
        assertEquals(1, warnings.size)
    }

    @Test
    fun `detection lists terminals and shells and is empty when the file is missing or broken`() {
        assertEquals(Detected(), settings.detected())
        write(
            DETECTED_FILE,
            """{"version": 1,
              "terminals": [{"id": "wezterm", "name": "WezTerm"}, {"id": "kitty"}, {"name": "no id"}, 7],
              "shells": [{"path": "/opt/pwsh", "label": "PowerShell 7.5.2 (MSI)", "source": "msi"}, {"path": "/opt/ps"}, {"label": "no path"}]}""",
        )
        assertEquals(
            Detected(
                listOf(DetectedTerminal("wezterm", "WezTerm"), DetectedTerminal("kitty", "kitty")),
                listOf(DetectedShell("/opt/pwsh", "PowerShell 7.5.2 (MSI)"), DetectedShell("/opt/ps", "/opt/ps")),
            ),
            settings.detected(),
        )
        write(DETECTED_FILE, """{"terminals": "none", "shells": {}}""")
        assertEquals(Detected(), settings.detected())
        write(DETECTED_FILE, "broken")
        assertEquals(Detected(), settings.detected())
    }

    @Test
    fun `launchVia defaults to direct and reads direct or ori`() {
        assertEquals(LaunchVia.DIRECT, settings.shared().launchVia)
        write(CONFIG_FILE, """{"launchVia": "ori"}""")
        assertEquals(LaunchVia.ORI, settings.shared().launchVia)
        write(CONFIG_FILE, """{"launchVia": "both"}""")
        assertEquals(LaunchVia.DIRECT, settings.shared().launchVia)
    }

    @Test
    fun `focusNewTabs defaults to auto, reads the three modes, and auto removes the key`() {
        assertEquals(FocusNewTabs.AUTO_FOCUS, settings.shared().focusNewTabs)
        for (mode in FocusNewTabs.entries) {
            write(CONFIG_FILE, """{"focusNewTabs": "${mode.value}"}""")
            assertEquals(mode, settings.shared().focusNewTabs)
        }
        write(CONFIG_FILE, """{"focusNewTabs": true}""")
        assertEquals(FocusNewTabs.AUTO_FOCUS, settings.shared().focusNewTabs)
        Files.writeString(config, """{"defaultAgent": "codex"}""")
        assertTrue(settings.setFocusNewTabs(FocusNewTabs.NEVER))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex", "focusNewTabs": "never"}"""), saved())
        assertTrue(settings.setFocusNewTabs(FocusNewTabs.AUTO_FOCUS))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex"}"""), saved())
    }

    @Test
    fun `claudeMod defaults to on, reads off, and on removes the key`() {
        assertTrue(settings.shared().claudeMod)
        write(CONFIG_FILE, """{"claudeMod": "off"}""")
        assertFalse(settings.shared().claudeMod)
        write(CONFIG_FILE, """{"claudeMod": "on"}""")
        assertTrue(settings.shared().claudeMod)
        write(CONFIG_FILE, """{"claudeMod": false}""")
        assertTrue(settings.shared().claudeMod)
        Files.writeString(config, """{"defaultAgent": "codex"}""")
        assertTrue(settings.setClaudeMod(false))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex", "claudeMod": "off"}"""), saved())
        assertTrue(settings.setClaudeMod(true))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex"}"""), saved())
    }

    @Test
    fun `launchVia saves to config and keeps other keys`() {
        Files.writeString(config, """{"defaultAgent": "codex", "launchVia": "ori"}""")
        assertTrue(settings.setLaunchVia(LaunchVia.DIRECT))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex", "launchVia": "direct"}"""), saved())
        assertTrue(settings.setLaunchVia(LaunchVia.ORI))
        assertEquals("ori", saved().get("launchVia").asString)
    }

    @Test
    fun `closeAfterHandoff defaults to true, reads a boolean and drops other values`() {
        assertTrue(settings.shared().closeAfterHandoff)
        write(CONFIG_FILE, """{"closeAfterHandoff": false}""")
        assertFalse(settings.shared().closeAfterHandoff)
        write(CONFIG_FILE, """{"closeAfterHandoff": "false"}""")
        assertTrue(settings.shared().closeAfterHandoff)
        write(CONFIG_FILE, """{"closeAfterHandoff": 0}""")
        assertTrue(settings.shared().closeAfterHandoff)
    }

    @Test
    fun `closeAfterHandoff writes false only when unchecked and removes the key when checked`() {
        Files.writeString(config, """{"defaultAgent": "codex"}""")
        assertTrue(settings.setCloseAfterHandoff(true))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex"}"""), saved())
        assertTrue(settings.setCloseAfterHandoff(false))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex", "closeAfterHandoff": false}"""), saved())
        assertTrue(settings.setCloseAfterHandoff(true))
        assertEquals(JsonParser.parseString("""{"defaultAgent": "codex"}"""), saved())
    }

    @Test
    fun `closeAfterHandoff leaves a broken config alone`() {
        Files.writeString(config, "{broken")
        assertFalse(settings.setCloseAfterHandoff(false))
        assertEquals("{broken", Files.readString(config))
        assertEquals(1, warnings.size)
    }

    @Test
    fun `detection reads the Ori entry and is null without it`() {
        write(DETECTED_FILE, """{"ori": {"path": "/x/ori", "version": "0.14.3", "agents": ["claude", 3, "codex"]}}""")
        assertEquals(DetectedOri("/x/ori", "0.14.3", listOf("claude", "codex")), settings.detected().ori)
        write(DETECTED_FILE, """{"ori": null}""")
        assertNull(settings.detected().ori)
        write(DETECTED_FILE, """{"ori": {"agents": ["claude"]}}""")
        assertNull(settings.detected().ori)
        write(DETECTED_FILE, """{"ori": []}""")
        assertNull(settings.detected().ori)
        write(DETECTED_FILE, """{"ori": {"path": "/x/ori"}}""")
        assertEquals(DetectedOri("/x/ori", null, emptyList()), settings.detected().ori)
    }
}
