package dev.alexk.ideagenttabs

import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.options.ConfigurationException
import com.intellij.openapi.ui.DialogPanel
import com.intellij.ui.dsl.builder.bindItem
import com.intellij.ui.dsl.builder.panel
import com.intellij.ui.dsl.listCellRenderer.listCellRenderer
import com.intellij.ui.dsl.listCellRenderer.textListCellRenderer

class AgentTabsConfigurable : BoundConfigurable("Agent Tabs") {

    private var saveFailed = false

    override fun createPanel(): DialogPanel = panel {
        row("Default agent:") {
            comboBox(Agents.settings.profiles(), listCellRenderer {
                val profile = value ?: return@listCellRenderer
                icon(Agents.icon(profile))
                text(profile.label)
            })
                .bindItem({ Agents.settings.defaultProfile() }, { profile ->
                    if (profile != null && profile != Agents.settings.defaultProfile()) saveFailed = !Agents.setDefaultAgent(profile)
                })
                .comment("Every IDE with Agent Tabs shares this setting, saved in ${configFile()}.")
        }
        row("Open on startup:") {
            comboBox(OpenOnStartup.entries, textListCellRenderer { it?.label })
                .bindItem({ AgentTabsOptions.getInstance().openOnStartup }, { mode ->
                    if (mode != null) AgentTabsOptions.getInstance().openOnStartup = mode
                })
                .comment("When to open the default agent in an editor tab as a project opens.")
        }
    }

    private fun configFile() = ideAgentTabsHome().resolve(CONFIG_FILE)

    override fun apply() {
        saveFailed = false
        super.apply()
        if (saveFailed) {
            throw ConfigurationException("Could not save the default agent to ${configFile()}. See idea.log for the reason.")
        }
    }
}
