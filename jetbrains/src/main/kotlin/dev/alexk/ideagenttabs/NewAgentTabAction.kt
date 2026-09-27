package dev.alexk.ideagenttabs

import com.intellij.ide.DataManager
import com.intellij.openapi.actionSystem.ActionGroup
import com.intellij.openapi.actionSystem.ActionToolbar
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.actionSystem.Presentation
import com.intellij.openapi.actionSystem.ex.CustomComponentAction
import com.intellij.openapi.actionSystem.impl.ActionButton
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.openapi.ui.popup.PopupStep
import com.intellij.openapi.ui.popup.util.BaseListPopupStep
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.Icon
import javax.swing.JComponent

class NewAgentTabAction : DumbAwareAction(), CustomComponentAction {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.project != null
        val label = Agents.settings.defaultProfile().label
        e.presentation.description = "Open $label in an editor tab, in the project root. Right-click to choose another agent."
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        Agents.open(project, Agents.settings.defaultProfile(), remember = false)
    }

    override fun createCustomComponent(presentation: Presentation, place: String): JComponent {
        val button = ActionButton(this, presentation, place, ActionToolbar.DEFAULT_MINIMUM_BUTTON_SIZE)
        button.addMouseListener(AgentMenuTrigger(button))
        return button
    }
}

// The popup trigger is a press on macOS and Linux and a release on Windows. On macOS, Ctrl+click is a
// left-button press, so its release would also run the button's own action unless it is consumed too.
private class AgentMenuTrigger(private val button: ActionButton) : MouseAdapter() {

    private var swallowRelease = false

    override fun mousePressed(e: MouseEvent) {
        if (e.isPopupTrigger) {
            e.consume()
            swallowRelease = true
            showAgentPopup(button)
        }
    }

    override fun mouseReleased(e: MouseEvent) {
        if (swallowRelease) {
            swallowRelease = false
            e.consume()
        } else if (e.isPopupTrigger) {
            e.consume()
            showAgentPopup(button)
        }
    }
}

private fun showAgentPopup(component: JComponent) {
    val project = DataManager.getInstance().getDataContext(component).getData(CommonDataKeys.PROJECT) ?: return
    ApplicationManager.getApplication().executeOnPooledThread {
        val profiles = Agents.installedProfiles()
        val icons = profiles.map(Agents::icon)
        ApplicationManager.getApplication().invokeLater({
            if (!component.isShowing || project.isDisposed) return@invokeLater
            val factory = JBPopupFactory.getInstance()
            val popup = if (profiles.isEmpty()) {
                factory.createMessage("No agent CLI found on PATH")
            } else {
                factory.createListPopup(AgentStep(project, profiles, icons))
            }
            popup.showUnderneathOf(component)
        }, ModalityState.stateForComponent(component))
    }
}

private class AgentStep(
    private val project: Project,
    profiles: List<AgentProfile>,
    icons: List<Icon>,
) : BaseListPopupStep<AgentProfile>("Open Agent", profiles, icons) {

    override fun getTextFor(value: AgentProfile) = value.label

    override fun onChosen(selectedValue: AgentProfile, finalChoice: Boolean): PopupStep<*>? =
        doFinalStep { if (!project.isDisposed) Agents.open(project, selectedValue, remember = true) }
}

class AgentMenuGroup : ActionGroup(), DumbAware {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun getChildren(e: AnActionEvent?): Array<AnAction> =
        Agents.installedProfiles().map { OpenAgentTabAction(it) }.toTypedArray()
}

private class OpenAgentTabAction(private val profile: AgentProfile) :
    DumbAwareAction(profile.label, "Open ${profile.label} in an editor tab and make it the default agent", Agents.icon(profile)) {

    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        Agents.open(project, profile, remember = true)
    }
}
