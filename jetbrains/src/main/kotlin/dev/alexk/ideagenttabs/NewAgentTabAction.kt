package dev.alexk.ideagenttabs

import com.intellij.ide.DataManager
import com.intellij.openapi.actionSystem.ActionGroup
import com.intellij.openapi.actionSystem.ActionToolbar
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.actionSystem.Presentation
import com.intellij.openapi.actionSystem.Separator
import com.intellij.openapi.actionSystem.ex.CustomComponentAction
import com.intellij.openapi.actionSystem.impl.ActionButton
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.DumbAwareAction
import javax.swing.JComponent

class NewAgentTabAction : DumbAwareAction(), CustomComponentAction {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.project != null
        val profile = Agents.settings.defaultProfile()
        e.presentation.icon = Agents.buttonIcon(profile)
        val label = viaLabel(profile.label, Agents.viaFor(profile))
        e.presentation.text = "New $label Tab"
        e.presentation.description = "Open $label in an editor tab, in the project root. Right-click to choose another agent."
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        Agents.open(project, Agents.settings.defaultProfile())
    }

    override fun createCustomComponent(presentation: Presentation, place: String): JComponent {
        val button = ActionButton(this, presentation, place, ActionToolbar.DEFAULT_MINIMUM_BUTTON_SIZE)
        button.addMouseListener(AgentMenuTrigger({
            val project = DataManager.getInstance().getDataContext(button).getData(CommonDataKeys.PROJECT)
            if (project != null) showAgentMenu(project, button) { it.showUnderneathOf(button) }
        }))
        return button
    }
}

class AgentMenuGroup : ActionGroup(), DumbAware {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun getChildren(e: AnActionEvent?): Array<AnAction> {
        val agents = Agents.rows().map { OpenAgentTabAction(it) }
        return (agents + listOf(Separator.getInstance(), OpenSettingsAction())).toTypedArray()
    }
}

private class OpenAgentTabAction(private val row: AgentRow) :
    DumbAwareAction(row.label, "Open ${row.label} in an editor tab", row.icon) {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        Agents.open(project, row.entry.profile, row.entry.via)
    }
}

private class OpenSettingsAction : DumbAwareAction("Settings…", "Open the Agent Tabs settings", null) {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        showAgentTabsSettings(e.project)
    }
}
