package dev.alexk.ideagenttabs

import org.junit.Rule
import org.junit.rules.ExternalResource
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.FileTime

val IS_WINDOWS = File.separatorChar == '\\'

val claude = AgentProfile("claude", "Claude Code", "claude", modelFlag = "--model")

class TempDirs : ExternalResource() {

    private val created = mutableListOf<Path>()

    fun create(prefix: String): Path = Files.createTempDirectory(prefix).also { created.add(it) }

    override fun after() {
        created.forEach { it.toFile().deleteRecursively() }
    }
}

abstract class SettingsFixture(prefix: String) {

    @get:Rule
    val temp = TempDirs()

    protected val home: Path = temp.create(prefix)
    protected val warnings = mutableListOf<String>()
    protected val settings = AgentSettings(home) { warnings += it }

    private var clock = System.currentTimeMillis()

    protected fun write(name: String, text: String) {
        val file = home.resolve(name)
        Files.writeString(file, text)
        clock += 10_000
        Files.setLastModifiedTime(file, FileTime.fromMillis(clock))
    }
}
