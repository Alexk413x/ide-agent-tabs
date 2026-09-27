package dev.alexk.ideagenttabs

import com.intellij.openapi.components.BaseState
import com.intellij.openapi.components.RoamingType
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.SimplePersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.wm.ToolWindowManager
import java.nio.file.Path

@Service(Service.Level.APP)
@State(name = "AgentTabs", storages = [Storage("agentTabs.xml", roamingType = RoamingType.DISABLED)])
class AgentTabsOptions : SimplePersistentStateComponent<AgentTabsOptions.Options>(Options()) {

    class Options : BaseState() {
        var openOnStartup by string(OpenOnStartup.CLAUDE_FOLDER.value)
    }

    var openOnStartup: OpenOnStartup
        get() = OpenOnStartup.of(state.openOnStartup)
        set(value) {
            state.openOnStartup = value.value
        }

    companion object {
        fun getInstance(): AgentTabsOptions = service()
    }
}

class OpenOnStartupActivity : ProjectActivity {
    override suspend fun execute(project: Project) {
        val base = project.basePath?.let { Path.of(it) }
        if (!opensOnStartup(AgentTabsOptions.getInstance().openOnStartup, base)) return
        val profile = Agents.settings.defaultProfile()
        // The tab starts in the Terminal tool window, which may not be registered yet when startup activities run.
        ToolWindowManager.getInstance(project).invokeLater {
            if (!project.isDisposed) Agents.open(project, profile)
        }
    }
}
