package dev.alexk.ideagenttabs

import com.intellij.openapi.fileChooser.FileChooser
import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory
import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.options.ConfigurationException
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.util.SystemInfo
import com.intellij.ui.CollectionComboBoxModel
import com.intellij.ui.dsl.builder.bindItem
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

    private fun saved(ok: Boolean) {
        if (!ok) saveFailed = true
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
        val detected = Agents.settings.detected()
        val shared = Agents.settings.shared()
        val terminalModel = choiceModel(detected.terminals.map { Choice(it.id, it.name) }, shared.terminal, custom = false)
        val shellModel = choiceModel(detected.shells.map { Choice(it.path, it.label) }, shared.shell, custom = true)
        return panel {
            row("Default agent:") {
                comboBox(Agents.settings.profiles(), listCellRenderer {
                    val profile = value ?: return@listCellRenderer
                    icon(Agents.icon(profile))
                    text(profile.label)
                })
                    .bindItem({ Agents.settings.defaultProfile() }, { profile ->
                        if (profile != null && profile != Agents.settings.defaultProfile()) saved(Agents.setDefaultAgent(profile))
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
            group("IDE tabs") {
                row("Open new tabs in:") {
                    comboBox(TabRouting.entries, textListCellRenderer { it?.label })
                        .bindItem({ Agents.settings.shared().tabRouting }, { value ->
                            if (value != null && value != Agents.settings.shared().tabRouting) saved(Agents.settings.setTabRouting(value))
                        })
                        .comment("Where a new agent tab opens when no IDE or terminal is named.")
                }
            }
            group("Terminal tabs") {
                row("Preferred terminal:") {
                    comboBox(terminalModel, textListCellRenderer { it?.label })
                        .bindItem({ terminalModel.items.firstOrNull { it.value == Agents.settings.shared().terminal } }, { choice ->
                            if (choice != null && choice.value != Agents.settings.shared().terminal) saved(Agents.settings.setTerminal(choice.value))
                        })
                        .comment("Terminal for agent tabs when no IDE is running or a terminal is asked for.")
                }
                if (SystemInfo.isWindows) {
                    row("Shell (Windows):") {
                        var previous: Choice = shellModel.items.firstOrNull { it.value == shared.shell } ?: AUTOMATIC
                        comboBox(shellModel, textListCellRenderer { it?.label })
                            .bindItem({ shellModel.items.firstOrNull { it.value == Agents.settings.shared().shell } }, { choice ->
                                if (choice != null && choice.value != Agents.settings.shared().shell) saved(Agents.settings.setShell(choice.value))
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
                row("Terminal window:") {
                    comboBox(TerminalWindow.entries, textListCellRenderer { it?.label })
                        .bindItem({ Agents.settings.shared().terminalWindow }, { value ->
                            if (value != null && value != Agents.settings.shared().terminalWindow) saved(Agents.settings.setTerminalWindow(value))
                        })
                        .comment("Whether terminal tabs join your last window or a window kept for Agent Tabs.")
                }
            }
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
