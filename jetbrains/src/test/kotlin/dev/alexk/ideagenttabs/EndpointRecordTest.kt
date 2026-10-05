package dev.alexk.ideagenttabs

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.FileTime
import java.nio.file.attribute.PosixFilePermissions
import java.time.Instant

class EndpointRecordTest {

    private val home: Path = Files.createTempDirectory("cst-home")
    private val token = newToken()
    private val entry = endpointJson("Android Studio", "2026.2.2", 12345, "http://127.0.0.1:63342/ide-agent-tabs", token, 1700000000000L)

    @Test
    fun `endpoint file holds exactly the registry fields`() {
        val file = writeAtomically(home.resolve("endpoints/jetbrains-12345.json"), entry.toString(), private = true)
        val json = JsonParser.parseString(Files.readString(file)).asJsonObject
        assertEquals(setOf("protocol", "ide", "product", "version", "pid", "url", "token", "startedAt", "beatMs"), json.keySet())
        assertEquals(1, json.get("protocol").asInt)
        assertEquals("jetbrains", json.get("ide").asString)
        assertEquals("Android Studio", json.get("product").asString)
        assertEquals("2026.2.2", json.get("version").asString)
        assertEquals(12345L, json.get("pid").asLong)
        assertEquals("http://127.0.0.1:63342/ide-agent-tabs", json.get("url").asString)
        assertEquals(token, json.get("token").asString)
        assertEquals(1700000000000L, json.get("startedAt").asLong)
        assertEquals(60000L, json.get("beatMs").asLong)
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
    fun `a beat touches the file and keeps its content`() {
        val target = writeAtomically(home.resolve("endpoints/jetbrains-4.json"), entry.toString(), private = true)
        Files.setLastModifiedTime(target, FileTime.fromMillis(System.currentTimeMillis() - 600_000))
        val now = Instant.now()
        beatEndpoint(target, "ignored", now = now)
        assertEquals(entry.toString(), Files.readString(target))
        assertTrue(Math.abs(Files.getLastModifiedTime(target).toMillis() - now.toEpochMilli()) < 2000)
    }

    @Test
    fun `a beat rewrites a deleted file`() {
        val target = home.resolve("endpoints/jetbrains-5.json")
        beatEndpoint(target, entry.toString())
        assertEquals(entry.toString(), Files.readString(target))
        assertEquals(listOf("jetbrains-5.json"), Files.list(target.parent).use { s -> s.map { it.fileName.toString() }.toList() })
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
