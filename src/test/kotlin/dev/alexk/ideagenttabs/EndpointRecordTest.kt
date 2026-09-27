package dev.alexk.ideagenttabs

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions

class EndpointRecordTest {

    private val home: Path = Files.createTempDirectory("cst-home")
    private val token = newToken()
    private val entry = endpointJson("Android Studio", "2026.2.2", 12345, "http://127.0.0.1:63342/ide-agent-tabs", token)

    @Test
    fun `endpoint file holds exactly the registry fields`() {
        val file = writeAtomically(home.resolve("endpoints/jetbrains-12345.json"), entry.toString(), private = true)
        val json = JsonParser.parseString(Files.readString(file)).asJsonObject
        assertEquals(setOf("protocol", "ide", "product", "version", "pid", "url", "token"), json.keySet())
        assertEquals(1, json.get("protocol").asInt)
        assertEquals("jetbrains", json.get("ide").asString)
        assertEquals("Android Studio", json.get("product").asString)
        assertEquals("2026.2.2", json.get("version").asString)
        assertEquals(12345L, json.get("pid").asLong)
        assertEquals("http://127.0.0.1:63342/ide-agent-tabs", json.get("url").asString)
        assertEquals(token, json.get("token").asString)
    }

    @Test
    fun `rewrite replaces the file and leaves no temporary files`() {
        val target = home.resolve("endpoints/jetbrains-1.json")
        writeAtomically(target, "first", private = true)
        writeAtomically(target, "second", private = true)
        assertEquals("second", Files.readString(target))
        assertEquals(listOf("jetbrains-1.json"), Files.list(target.parent).use { s -> s.map { it.fileName.toString() }.toList() })
    }

    @Test
    fun `endpoint folder and file are private on POSIX`() {
        assumeTrue("POSIX permissions are tested on macOS and Linux", File.separatorChar != '\\')
        val dir = home.resolve("endpoints")
        Files.createDirectories(dir)
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxr-xr-x"))
        val file = writeAtomically(dir.resolve("jetbrains-2.json"), entry.toString(), private = true)
        assertEquals("rwx------", PosixFilePermissions.toString(Files.getPosixFilePermissions(dir)))
        assertEquals("rw-------", PosixFilePermissions.toString(Files.getPosixFilePermissions(file)))

        val fresh = writeAtomically(home.resolve("new/endpoints/jetbrains-3.json"), entry.toString(), private = true)
        assertEquals("rwx------", PosixFilePermissions.toString(Files.getPosixFilePermissions(fresh.parent)))
        assertEquals("rw-------", PosixFilePermissions.toString(Files.getPosixFilePermissions(fresh)))
    }

    @Test
    fun `home folder follows the system property`() {
        val previous = System.getProperty(HOME_PROPERTY)
        try {
            System.setProperty(HOME_PROPERTY, home.toString())
            assertEquals(home, ideAgentTabsHome())
            System.clearProperty(HOME_PROPERTY)
            assertEquals(Path.of(System.getProperty("user.home"), ".ide-agent-tabs"), ideAgentTabsHome())
        } finally {
            if (previous == null) System.clearProperty(HOME_PROPERTY) else System.setProperty(HOME_PROPERTY, previous)
        }
    }
}
