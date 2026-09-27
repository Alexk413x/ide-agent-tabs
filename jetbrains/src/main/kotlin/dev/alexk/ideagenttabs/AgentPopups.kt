package dev.alexk.ideagenttabs

import com.intellij.icons.AllIcons
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.JBPopup
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.openapi.ui.popup.ListPopupStepEx
import com.intellij.openapi.ui.popup.ListSeparator
import com.intellij.openapi.ui.popup.PopupStep
import com.intellij.openapi.ui.popup.util.BaseListPopupStep
import com.intellij.util.ui.StatusText
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.Icon
import javax.swing.JComponent
import javax.swing.SwingUtilities

const val NO_AGENT_FOUND = "No agent CLI found on PATH"

// The popup trigger is a press on macOS and Linux and a release on Windows. On macOS, Ctrl+click is a
// left-button press, so its release would also run the click action unless it is consumed too.
class AgentMenuTrigger(private val showMenu: () -> Unit, private val onClick: () -> Unit = {}) : MouseAdapter() {

    private var swallowRelease = false

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
        } else if (SwingUtilities.isLeftMouseButton(e) && e.component.contains(e.point)) {
            onClick()
        }
    }
}

fun showAgentMenu(project: Project, component: JComponent, place: (JBPopup) -> Unit) {
    withInstalledAgents(project, component) { profiles, icons ->
        val items = profiles.zip(icons, AgentMenuItem::Open) + listOf(AgentMenuItem.SetDefault, AgentMenuItem.Settings)
        place(JBPopupFactory.getInstance().createListPopup(AgentMenuStep(project, component, place, items)))
    }
}

fun showDefaultAgentChooser(project: Project, component: JComponent?, place: (JBPopup) -> Unit) {
    withInstalledAgents(project, component) { profiles, icons ->
        val factory = JBPopupFactory.getInstance()
        place(if (profiles.isEmpty()) factory.createMessage(NO_AGENT_FOUND) else factory.createListPopup(DefaultAgentStep(project, profiles, icons)))
    }
}

fun showAgentTabsSettings(project: Project?) {
    ShowSettingsUtil.getInstance().showSettingsDialog(project, AgentTabsConfigurable::class.java)
}

private fun withInstalledAgents(project: Project, component: JComponent?, then: (List<AgentProfile>, List<Icon>) -> Unit) {
    val modality = component?.let(ModalityState::stateForComponent) ?: ModalityState.nonModal()
    ApplicationManager.getApplication().executeOnPooledThread {
        val profiles = Agents.installedProfiles()
        val icons = profiles.map(Agents::icon)
        ApplicationManager.getApplication().invokeLater({
            if (project.isDisposed || component?.isShowing == false) return@invokeLater
            then(profiles, icons)
        }, modality)
    }
}

private sealed interface AgentMenuItem {
    class Open(val profile: AgentProfile, val icon: Icon) : AgentMenuItem
    data object SetDefault : AgentMenuItem
    data object Settings : AgentMenuItem
}

private class AgentMenuStep(
    private val project: Project,
    private val component: JComponent,
    private val place: (JBPopup) -> Unit,
    items: List<AgentMenuItem>,
) : BaseListPopupStep<AgentMenuItem>(if (items.first() is AgentMenuItem.Open) "Open Agent" else NO_AGENT_FOUND, items) {

    override fun getTextFor(value: AgentMenuItem) = when (value) {
        is AgentMenuItem.Open -> value.profile.label
        AgentMenuItem.SetDefault -> "Set Default Agent…"
        AgentMenuItem.Settings -> "Settings…"
    }

    override fun getIconFor(value: AgentMenuItem): Icon = when (value) {
        is AgentMenuItem.Open -> value.icon
        AgentMenuItem.SetDefault -> AllIcons.Nodes.Favorite
        AgentMenuItem.Settings -> AllIcons.General.Settings
    }

    override fun getSeparatorAbove(value: AgentMenuItem): ListSeparator? =
        if (value == AgentMenuItem.SetDefault && values.first() != value) ListSeparator() else null

    override fun onChosen(selectedValue: AgentMenuItem, finalChoice: Boolean): PopupStep<*>? = doFinalStep {
        if (project.isDisposed) return@doFinalStep
        when (selectedValue) {
            is AgentMenuItem.Open -> Agents.open(project, selectedValue.profile)
            AgentMenuItem.SetDefault -> showDefaultAgentChooser(project, component, place)
            AgentMenuItem.Settings -> showAgentTabsSettings(project)
        }
    }
}

private class DefaultAgentStep(
    private val project: Project,
    profiles: List<AgentProfile>,
    icons: List<Icon>,
) : BaseListPopupStep<AgentProfile>("Set Default Agent", profiles, icons), ListPopupStepEx<AgentProfile> {

    private val current = Agents.settings.defaultProfile().name

    init {
        defaultOptionIndex = profiles.indexOfFirst { it.name == current }.coerceAtLeast(0)
    }

    override fun getTextFor(value: AgentProfile) = value.label

    override fun getSecondaryTextFor(value: AgentProfile): String? = if (value.name == current) "current default" else null

    override fun getTooltipTextFor(value: AgentProfile): String? = null

    override fun setEmptyText(emptyText: StatusText) {}

    override fun onChosen(selectedValue: AgentProfile, finalChoice: Boolean): PopupStep<*>? = doFinalStep {
        if (!Agents.setDefaultAgent(selectedValue) && !project.isDisposed) {
            Messages.showErrorDialog(
                project,
                "Could not save the default agent to ${ideAgentTabsHome().resolve(CONFIG_FILE)}. See idea.log for the reason.",
                "Set Default Agent",
            )
        }
    }
}
