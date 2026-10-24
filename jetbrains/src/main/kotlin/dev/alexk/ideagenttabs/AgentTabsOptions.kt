package dev.alexk.ideagenttabs

import com.intellij.openapi.components.BaseState
import com.intellij.openapi.components.RoamingType
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.SimplePersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service

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
