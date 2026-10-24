package dev.alexk.ideagenttabs

import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.wm.ToolWindowManager
import java.nio.file.Path

class OpenOnStartupActivity : ProjectActivity {
    override suspend fun execute(project: Project) {
        val base = project.basePath?.let { Path.of(it) }
        val profile = if (opensOnStartup(AgentTabsOptions.getInstance().openOnStartup, base)) Agents.settings.defaultProfile() else null
        // The tab starts in the Terminal tool window, which may not be registered yet when startup activities run.
        // Tabs found open mean the plugin was reloaded into a running project, not that the project opened.
        ToolWindowManager.getInstance(project).invokeLater {
            if (project.isDisposed) return@invokeLater
            if (AgentTabRegistry.getInstance().revive(project) == 0 && profile != null) Agents.open(project, profile)
        }
    }
}
