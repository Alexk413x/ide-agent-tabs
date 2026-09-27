package dev.alexk.ideagenttabs

import com.intellij.openapi.application.ApplicationActivationListener
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.popup.PopupShowOptions
import com.intellij.openapi.util.IconLoader
import com.intellij.openapi.wm.CustomStatusBarWidget
import com.intellij.openapi.wm.IdeFrame
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.JBUI
import javax.swing.JComponent

class AgentStatusBarWidgetFactory : StatusBarWidgetFactory {

    override fun getId() = AgentStatusBarWidget.ID

    override fun getDisplayName() = "Agent Tabs"

    override fun createWidget(project: Project): StatusBarWidget = AgentStatusBarWidget(project)
}

class AgentStatusBarWidget(private val project: Project) : CustomStatusBarWidget {

    private val label = JBLabel(ICON).apply { border = JBUI.CurrentTheme.StatusBar.Widget.border() }

    init {
        label.addMouseListener(AgentMenuTrigger(
            showMenu = { showAgentMenu(project, label) { it.show(PopupShowOptions.aboveComponent(label)) } },
            onClick = { if (!project.isDisposed) Agents.open(project, Agents.settings.defaultProfile()) },
        ))
        val connection = ApplicationManager.getApplication().messageBus.connect(this)
        connection.subscribe(DefaultAgentListener.TOPIC, DefaultAgentListener(::refresh))
        connection.subscribe(ApplicationActivationListener.TOPIC, object : ApplicationActivationListener {
            override fun applicationActivated(ideFrame: IdeFrame) = refresh()
        })
        refresh()
    }

    override fun ID() = ID

    override fun getComponent(): JComponent = label

    private fun refresh() {
        val profile = Agents.settings.defaultProfile()
        label.text = profile.label
        label.toolTipText = "Open ${profile.label} in an editor tab. Right-click for other agents."
    }

    companion object {
        const val ID = "AgentTabs.StatusBar"

        private val ICON = IconLoader.getIcon("/icons/agentTabBrain.svg", AgentStatusBarWidget::class.java)
    }
}
