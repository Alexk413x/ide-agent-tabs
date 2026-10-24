package dev.alexk.ideagenttabs

import com.intellij.icons.AllIcons
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.popup.JBPopup
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.openapi.ui.popup.ListSeparator
import com.intellij.openapi.ui.popup.PopupStep
import com.intellij.openapi.ui.popup.util.BaseListPopupStep
import com.intellij.ui.PopupHandler
import java.awt.Component
import java.awt.event.MouseEvent
import javax.swing.Icon
import javax.swing.JComponent

const val NO_AGENT_FOUND = "No agent CLI found on PATH"

// The popup trigger is a press on macOS and Linux and a release on Windows. On macOS, Ctrl+click is a
// left-button press, so its release would also run the click action unless it is consumed too.
// Extends PopupHandler because a toolbar adds its Customize Toolbar menu to any child without one.
class AgentMenuTrigger(private val showMenu: () -> Unit) : PopupHandler() {

    private var swallowRelease = false

    override fun invokePopup(comp: Component, x: Int, y: Int) = showMenu()

    override fun mouseClicked(e: MouseEvent) {
        if (e.isPopupTrigger) e.consume()
    }

    override fun mousePressed(e: MouseEvent) {
        if (e.isPopupTrigger) {
            e.consume()
            swallowRelease = true
            showMenu()
        }
    }

    override fun mouseReleased(e: MouseEvent) {
        if (swallowRelease) {
            swallowRelease = false
            e.consume()
        } else if (e.isPopupTrigger) {
            e.consume()
            showMenu()
        }
    }
}

fun showAgentMenu(project: Project, component: JComponent, place: (JBPopup) -> Unit) {
    withInstalledAgents(project, component) { rows ->
        val items = rows.map { AgentMenuItem.Open(it) } + AgentMenuItem.Settings
        place(JBPopupFactory.getInstance().createListPopup(AgentMenuStep(project, items)))
    }
}

fun showAgentTabsSettings(project: Project?) {
    ShowSettingsUtil.getInstance().showSettingsDialog(project, AgentTabsConfigurable::class.java)
}

private fun withInstalledAgents(project: Project, component: JComponent, then: (List<AgentRow>) -> Unit) {
    val modality = ModalityState.stateForComponent(component)
    ApplicationManager.getApplication().executeOnPooledThread {
        val rows = Agents.rows()
        ApplicationManager.getApplication().invokeLater({
            if (project.isDisposed || !component.isShowing) return@invokeLater
            then(rows)
        }, modality)
    }
}

private sealed interface AgentMenuItem {
    class Open(val row: AgentRow) : AgentMenuItem
    data object Settings : AgentMenuItem
}

private class AgentMenuStep(
    private val project: Project,
    items: List<AgentMenuItem>,
) : BaseListPopupStep<AgentMenuItem>(if (items.first() is AgentMenuItem.Open) "Open Agent" else NO_AGENT_FOUND, items) {

    override fun getTextFor(value: AgentMenuItem) = when (value) {
        is AgentMenuItem.Open -> value.row.label
        AgentMenuItem.Settings -> "Settings…"
    }

    override fun getIconFor(value: AgentMenuItem): Icon = when (value) {
        is AgentMenuItem.Open -> value.row.icon
        AgentMenuItem.Settings -> AllIcons.General.Settings
    }

    override fun getSeparatorAbove(value: AgentMenuItem): ListSeparator? =
        if (value == AgentMenuItem.Settings && values.first() != value) ListSeparator() else null

    override fun onChosen(selectedValue: AgentMenuItem, finalChoice: Boolean): PopupStep<*>? = doFinalStep {
        if (project.isDisposed) return@doFinalStep
        when (selectedValue) {
            is AgentMenuItem.Open -> Agents.open(project, selectedValue.row.entry.profile, selectedValue.row.entry.via)
            AgentMenuItem.Settings -> showAgentTabsSettings(project)
        }
    }
}
