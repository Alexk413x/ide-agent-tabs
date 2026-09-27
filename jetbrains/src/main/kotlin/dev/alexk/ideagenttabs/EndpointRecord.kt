package dev.alexk.ideagenttabs

import com.google.gson.JsonObject
import java.nio.file.FileSystems
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.PosixFilePermissions
import java.security.SecureRandom
import java.util.HexFormat

// Pure module: no IDE or Netty types, so the registry file tests without a running IDE.

const val PROTOCOL_VERSION = 1
const val HOME_PROPERTY = "ide.agent.tabs.home"

fun ideAgentTabsHome(): Path =
    System.getProperty(HOME_PROPERTY)?.let { Path.of(it) } ?: Path.of(System.getProperty("user.home"), ".ide-agent-tabs")

fun newToken(): String = ByteArray(32).also { SecureRandom().nextBytes(it) }.let { HexFormat.of().formatHex(it) }

fun endpointJson(product: String, version: String, pid: Long, url: String, token: String) = JsonObject().apply {
    addProperty("protocol", PROTOCOL_VERSION)
    addProperty("ide", "jetbrains")
    addProperty("product", product)
    addProperty("version", version)
    addProperty("pid", pid)
    addProperty("url", url)
    addProperty("token", token)
}

private val isPosix = "posix" in FileSystems.getDefault().supportedFileAttributeViews()

// Windows gets no ACL change: the user profile's inherited permissions already exclude other users.
fun writeAtomically(target: Path, content: String, private: Boolean = false): Path {
    val dir = target.toAbsolutePath().parent
    val secure = private && isPosix
    if (secure) {
        Files.createDirectories(dir, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")))
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwx------"))
    } else {
        Files.createDirectories(dir)
    }
    val name = target.fileName.toString()
    val temp = if (secure) {
        Files.createTempFile(dir, name, ".tmp", PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
    } else {
        Files.createTempFile(dir, name, ".tmp")
    }
    try {
        Files.writeString(temp, content)
        Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    } catch (e: Exception) {
        Files.deleteIfExists(temp)
        throw e
    }
    return target
}
