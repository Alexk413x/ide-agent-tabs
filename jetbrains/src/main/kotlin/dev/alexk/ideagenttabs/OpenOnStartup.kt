package dev.alexk.ideagenttabs

import java.nio.file.Files
import java.nio.file.Path

// Pure module: no IDE types, so the startup decision tests without a running IDE.

enum class OpenOnStartup(val value: String, val label: String) {
    CLAUDE_FOLDER("claudeFolder", "When the project has a .claude folder"),
    ALWAYS("always", "Always"),
    NEVER("never", "Never");

    companion object {
        fun of(value: String?): OpenOnStartup = entries.firstOrNull { it.value == value } ?: CLAUDE_FOLDER
    }
}

fun opensOnStartup(mode: OpenOnStartup, projectDir: Path?): Boolean = when {
    projectDir == null -> false
    mode == OpenOnStartup.ALWAYS -> true
    mode == OpenOnStartup.CLAUDE_FOLDER -> Files.isDirectory(projectDir.resolve(".claude"))
    else -> false
}
