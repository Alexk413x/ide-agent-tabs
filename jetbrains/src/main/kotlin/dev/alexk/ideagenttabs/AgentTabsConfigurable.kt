package dev.alexk.ideagenttabs

import com.intellij.openapi.fileChooser.FileChooser
import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory
import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.options.ConfigurationException
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.util.SystemInfo
import com.intellij.ui.CollectionComboBoxModel
import com.intellij.ui.dsl.builder.Cell
import com.intellij.ui.dsl.builder.Panel
import com.intellij.ui.dsl.builder.Row
import com.intellij.ui.dsl.builder.bindItem
import com.intellij.ui.dsl.builder.bindSelected
import com.intellij.ui.dsl.builder.panel
import com.intellij.ui.dsl.listCellRenderer.listCellRenderer
import com.intellij.ui.dsl.listCellRenderer.textListCellRenderer

private class Choice(val value: String, val label: String) {
    override fun equals(other: Any?) = other is Choice && other.value == value
    override fun hashCode() = value.hashCode()
}

private val AUTOMATIC = Choice(AUTO, "Automatic")
private val CUSTOM_PATH = Choice("\u0000custom", "Custom path…")

class AgentTabsConfigurable : BoundConfigurable("Agent Tabs") {

    private var saveFailed = false

    private val settings: AgentSettings get() = Agents.settings

    private fun <T> change(value: T, current: T, set: (T) -> Boolean) {
        if (value != current && !set(value)) saveFailed = true
    }

    private fun choiceModel(detected: List<Choice>, current: String, custom: Boolean): CollectionComboBoxModel<Choice> {
        val items = mutableListOf(AUTOMATIC)
        items += detected
        if (current != AUTO && items.none { it.value == current }) items += Choice(current, "$current (not detected)")
        if (custom) items += CUSTOM_PATH
        return CollectionComboBoxModel(items)
    }

    private fun pickShell(): Choice? {
        val file = FileChooser.chooseFile(FileChooserDescriptorFactory.createSingleFileNoJarsDescriptor().withTitle("Shell"), null, null)
        return file?.let { Choice(it.toNioPath().toString(), it.toNioPath().toString()) }
    }

    override fun createPanel(): DialogPanel {
        val detected = settings.detected()
        return panel {
            defaultAgentRow()
            if (detected.ori != null) {
                flagRow(
                    "Show Ori agents",
                    "List an Ori entry, such as Claude Code - Ori, under each agent that Ori can launch, in the New Agent Tab menus and in list_agents. It opens the agent with <code>ori &lt;agent&gt;</code> in its own tab.",
                    SharedSettings::showOriAgents,
                    settings::setShowOriAgents,
                )
            }
            flagRow(
                "Show LM Studio agents",
                "List Codex - LM Studio under Codex, in the New Agent Tab menus and in list_agents. It opens Codex with <code>--oss --local-provider lmstudio</code>, which runs a local model in LM Studio.",
                SharedSettings::showLmStudioAgents,
                settings::setShowLmStudioAgents,
            )
            flagRow(
                "Show Ollama agents",
                "List Codex - Ollama under Codex, in the New Agent Tab menus and in list_agents. It opens Codex with <code>--oss --local-provider ollama</code>, which runs a local model in Ollama 0.13.4 or later.",
                SharedSettings::showOllamaAgents,
                settings::setShowOllamaAgents,
            )
            flagRow(
                "Close the old tab after a handoff",
                "After a handoff, the new session closes the old tab once both sides confirm. Off leaves the old tab open, marked as handed off.",
                SharedSettings::closeAfterHandoff,
                settings::setCloseAfterHandoff,
            )
            flagRow(
                "Allow resuming closed sessions",
                "Agents can reopen a Claude Code, Codex or Antigravity CLI session that ended in the last 7 days. A resume past the prompt cache re-reads the whole history at full price, so the agent asks you first. Off refuses every resume.",
                SharedSettings::allowResume,
                settings::setAllowResume,
            )
            flagRow(
                "Use the Claude Code mod (in-process messaging)",
                "Claude Code sessions message other agents with SendMessage and ListAgents and get their messages in-process. Off uses the hooks and wake lines. New sessions pick up a change.",
                SharedSettings::claudeMod,
                settings::setClaudeMod,
            )
            row("Open on startup:") {
                comboBox(OpenOnStartup.entries, textListCellRenderer { it?.label })
                    .bindItem({ AgentTabsOptions.getInstance().openOnStartup }, { mode ->
                        if (mode != null) AgentTabsOptions.getInstance().openOnStartup = mode
                    })
                    .comment("When to open the default agent in an editor tab as a project opens.")
            }
            group("IDE tabs") { ideTabRows() }
            group("Terminal tabs") { terminalRows(detected) }
        }
    }

    private fun Panel.defaultAgentRow() {
        row("Default agent:") {
            comboBox(settings.profiles(), listCellRenderer {
                val profile = value ?: return@listCellRenderer
                icon(Agents.icon(profile))
                text(profile.label)
            })
                .bindItem({ settings.defaultProfile() }, { profile ->
                    if (profile != null) change(profile, settings.defaultProfile()) { settings.setDefaultAgent(it.name) }
                })
                .comment("Every IDE with Agent Tabs shares this setting, saved in ${configFile()}.")
        }
    }

    private fun Panel.flagRow(text: String, comment: String, current: (SharedSettings) -> Boolean, set: (Boolean) -> Boolean) {
        row {
            checkBox(text)
                .bindSelected({ current(settings.shared()) }, { value -> change(value, current(settings.shared()), set) })
                .comment(comment)
        }
    }

    private fun <T : Any> Row.choiceCombo(entries: List<T>, label: (T) -> String, current: () -> T, set: (T) -> Boolean): Cell<ComboBox<T>> =
        comboBox(entries, textListCellRenderer { it?.let(label) })
            .bindItem({ current() }, { value -> if (value != null) change(value, current(), set) })

    private fun Panel.ideTabRows() {
        row("Open new tabs in:") {
            choiceCombo(TabRouting.entries, TabRouting::label, { settings.shared().tabRouting }, settings::setTabRouting)
                .comment("Where a new agent tab opens when no IDE or terminal is named.")
        }
        row("Bring new agent tabs to the front:") {
            choiceCombo(FocusNewTabs.entries, FocusNewTabs::label, { settings.shared().focusNewTabs }, settings::setFocusNewTabs)
                .comment("For tabs that agents open. An agent's tab opens behind the current one unless you asked for it. The New Agent Tab button always brings its tab to the front.")
        }
    }

    private fun Panel.terminalRows(detected: Detected) {
        val terminalModel = choiceModel(detected.terminals.map { Choice(it.id, it.name) }, settings.shared().terminal, custom = false)
        row("Preferred terminal:") {
            comboBox(terminalModel, textListCellRenderer { it?.label })
                .bindItem({ terminalModel.items.firstOrNull { it.value == settings.shared().terminal } }, { choice ->
                    if (choice != null) change(choice.value, settings.shared().terminal, settings::setTerminal)
                })
                .comment("Terminal for agent tabs when no IDE is running or a terminal is asked for.")
        }
        if (SystemInfo.isWindows) shellRow(detected)
        row("Terminal window:") {
            choiceCombo(TerminalWindow.entries, TerminalWindow::label, { settings.shared().terminalWindow }, settings::setTerminalWindow)
                .comment("Whether terminal tabs join your last window or a window kept for Agent Tabs.")
        }
    }

    private fun Panel.shellRow(detected: Detected) {
        val shellModel = choiceModel(detected.shells.map { Choice(it.path, it.label) }, settings.shared().shell, custom = true)
        row("Shell (Windows):") {
            var previous: Choice = shellModel.items.firstOrNull { it.value == settings.shared().shell } ?: AUTOMATIC
            comboBox(shellModel, textListCellRenderer { it?.label })
                .bindItem({ shellModel.items.firstOrNull { it.value == settings.shared().shell } }, { choice ->
                    if (choice != null) change(choice.value, settings.shared().shell, settings::setShell)
                })
                .applyToComponent {
                    addActionListener {
                        val selected = shellModel.selected ?: return@addActionListener
                        if (selected != CUSTOM_PATH) {
                            previous = selected
                            return@addActionListener
                        }
                        val chosen = pickShell()
                        if (chosen == null) {
                            shellModel.selectedItem = previous
                            return@addActionListener
                        }
                        val listed = shellModel.items.filter { it != CUSTOM_PATH }
                        shellModel.replaceAll(if (chosen in listed) listed + CUSTOM_PATH else listed + chosen + CUSTOM_PATH)
                        shellModel.selectedItem = chosen
                    }
                }
                .comment("PowerShell that runs agent tabs in a terminal.")
        }
    }

    private fun configFile() = ideAgentTabsHome().resolve(CONFIG_FILE)

    override fun apply() {
        saveFailed = false
        super.apply()
        if (saveFailed) {
            throw ConfigurationException("Could not save the settings to ${configFile()}. See idea.log for the reason.")
        }
    }
}
