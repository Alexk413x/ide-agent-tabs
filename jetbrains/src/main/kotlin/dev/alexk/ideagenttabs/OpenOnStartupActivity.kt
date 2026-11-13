package dev.alexk.ideagenttabs

import com.intellij.ide.trustedProjects.TrustedProjects
import com.intellij.ide.trustedProjects.TrustedProjectsListener
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.wm.ToolWindowManager
import java.nio.file.Path
import java.util.concurrent.atomic.AtomicBoolean

class OpenOnStartupActivity : ProjectActivity {
    override suspend fun execute(project: Project) {
        val base = project.basePath?.let { Path.of(it) }
        val action = startupAction(AgentTabsOptions.getInstance().openOnStartup, base, TrustedProjects.isProjectTrusted(project))
        val profile = if (action == Startup.NONE) null else Agents.settings.defaultProfile()
        // The tab starts in the Terminal tool window, which may not be registered yet when startup activities run.
        // Tabs found open mean the plugin was reloaded into a running project, not that the project opened.
        ToolWindowManager.getInstance(project).invokeLater {
            if (project.isDisposed) return@invokeLater
            if (AgentTabRegistry.getInstance().revive(project) > 0 || profile == null) return@invokeLater
            if (action == Startup.OPEN) Agents.open(project, profile) else openWhenTrusted(project, profile)
        }
    }

    private fun openWhenTrusted(project: Project, profile: AgentProfile) {
        val opened = AtomicBoolean(false)
        val connection = ApplicationManager.getApplication().messageBus.connect(project)
        connection.subscribe(TrustedProjectsListener.TOPIC, object : TrustedProjectsListener {
            override fun onProjectTrusted(trusted: Project) {
                if (trusted !== project) return
                connection.disconnect()
                if (!opened.compareAndSet(false, true)) return
                ToolWindowManager.getInstance(project).invokeLater {
                    if (!project.isDisposed) Agents.open(project, profile)
                }
            }
        })
        // Trust granted between the check in execute and the subscription would otherwise go unnoticed.
        if (TrustedProjects.isProjectTrusted(project)) {
            connection.disconnect()
            if (opened.compareAndSet(false, true)) Agents.open(project, profile)
        }
    }
}
