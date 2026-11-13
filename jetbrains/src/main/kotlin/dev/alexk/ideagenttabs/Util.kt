package dev.alexk.ideagenttabs

import java.nio.file.FileSystems
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.PosixFilePermissions
import java.security.SecureRandom
import java.util.HexFormat

// Pure module: no IDE or Netty types, so the shared helpers test without a running IDE.

const val HOME_PROPERTY = "ide.agent.tabs.home"
const val HOME_ENV = "IDE_AGENT_TABS_HOME"

// The system property (tests and the sandbox IDE) wins over the environment variable that VS Code and the MCP
// server read; a blank value counts as unset.
fun homeOverride(property: String? = System.getProperty(HOME_PROPERTY), env: String? = System.getenv(HOME_ENV)): Path? =
    listOf(property, env).firstOrNull { !it.isNullOrBlank() }?.let { Path.of(it) }

fun ideAgentTabsHome(override: Path? = homeOverride()): Path = override ?: Path.of(System.getProperty("user.home"), ".ide-agent-tabs")

fun newToken(): String = ByteArray(32).also { SecureRandom().nextBytes(it) }.let { HexFormat.of().formatHex(it) }

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
