package dev.alexk.ideagenttabs

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.IconLoader
import com.intellij.openapi.util.SystemInfo
import com.intellij.ui.LayeredIcon
import com.intellij.util.EnvironmentUtil
import com.intellij.util.IconUtil
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import javax.swing.Icon

const val VIA_LABEL = "via OpenRouter"

fun viaLabel(text: String, via: LaunchVia) = if (via == LaunchVia.ORI) "$text ($VIA_LABEL)" else text

class AgentRow(val entry: AgentEntry, val icon: Icon, val label: String)

object Agents {

    private val LOG = logger<Agents>()

    private val warn: (String) -> Unit = { LOG.warn(it) }

    val settings = AgentSettings(ideAgentTabsHome(), warn)

    private val neutralIcon: Icon = IconLoader.getIcon("/icons/agentTab.svg", Agents::class.java)

    private val builtinIcons = mapOf(
        "claude" to "/icons/agents/claude.svg",
        "codex" to "/icons/agents/codex.svg",
        "copilot" to "/icons/agents/copilot.svg",
        "agy" to "/icons/agents/agy.svg",
        "grok" to "/icons/agents/grok.svg",
        "pi" to "/icons/agents/pi.svg",
        "hermes" to "/icons/agents/hermes.svg",
        "opencode" to "/icons/agents/opencode.svg",
        "qwen" to "/icons/agents/qwen.svg",
        "goose" to "/icons/agents/goose.svg",
        "codex-lmstudio" to "/icons/agents/codex.svg",
        "codex-ollama" to "/icons/agents/codex.svg",
    )

    private val neutralButtonIcon: Icon = IconLoader.getIcon("/icons/agentTab_new.svg", Agents::class.java)

    private val badge: Icon = IconLoader.getIcon("/icons/newBadge.svg", Agents::class.java)

    private val customIcons = ConcurrentHashMap<String, Icon>()

    private val customButtonIcons = ConcurrentHashMap<String, Icon>()

    val searchPath: String
        get() = EnvironmentUtil.getValue("PATH") ?: System.getenv("PATH").orEmpty()

    fun launchFor(
        profile: AgentProfile,
        request: OpenRequest? = null,
        cwd: String? = request?.path?.toString(),
        tabId: String = UUID.randomUUID().toString(),
        copilot: CopilotHttp? = null,
        via: LaunchVia? = request?.via,
    ): AgentLaunch =
        planLaunch(
            profile,
            LaunchContext(
                prompt = request?.prompt,
                args = request?.args.orEmpty(),
                env = request?.env.orEmpty(),
                model = request?.model,
                via = via,
                setting = settings.shared().launchVia,
                ori = settings.detected().ori,
                windows = SystemInfo.isWindows,
                searchPath = searchPath,
                python = codexPython(ideAgentTabsHome(), SystemInfo.isWindows, warn),
                http = codexHttp(ideAgentTabsHome(), SystemInfo.isWindows, tabId, profile.name, warn),
                copilot = copilot,
                claudeSettings = claudeTabSettings(ideAgentTabsHome(), SystemInfo.isWindows),
                cwd = cwd,
                warn = warn,
            ),
        )

    fun copilotFor(profile: AgentProfile): CopilotHttp? {
        if (!isCopilotCommand(profile.command)) return null
        val copilotHome = EnvironmentUtil.getValue("COPILOT_HOME") ?: System.getenv("COPILOT_HOME")
        return copilotHttp(ideAgentTabsHome(), SystemInfo.isWindows, copilotHooksFile(copilotHome, System.getProperty("user.home")), warn)
    }

    fun isInstalled(profile: AgentProfile): Boolean = isInstalled(profile.command, searchPath, SystemInfo.isWindows)

    fun entries(): List<AgentEntry> {
        val path = searchPath
        return agentEntries(settings.profiles(), settings.detected().ori, settings.shared()) {
            isInstalled(it.command, path, SystemInfo.isWindows)
        }
    }

    fun rows(): List<AgentRow> = entries().map { AgentRow(it, icon(it.profile), entryLabel(it)) }

    fun viaFor(profile: AgentProfile): LaunchVia = planVia(
        profile,
        LaunchContext(
            setting = settings.shared().launchVia,
            ori = settings.detected().ori,
            windows = SystemInfo.isWindows,
            searchPath = searchPath,
        ),
    )

    fun entryLabel(entry: AgentEntry): String = if (entry.via == null) viaLabel(entry.label, viaFor(entry.profile)) else entry.label

    fun icon(profile: AgentProfile): Icon {
        profile.icon?.let { return customIcons.computeIfAbsent(it, ::loadCustomIcon) }
        return builtinIcons[profile.name]?.let { IconLoader.getIcon(it, Agents::class.java) } ?: neutralIcon
    }

    fun buttonIcon(profile: AgentProfile): Icon {
        profile.icon?.let { path ->
            return customButtonIcons.computeIfAbsent(path) {
                val icon = icon(profile)
                if (icon === neutralIcon) neutralButtonIcon else LayeredIcon.layeredIcon(arrayOf(IconUtil.resizeSquared(icon, 12), badge))
            }
        }
        return builtinIcons[profile.name]?.let { IconLoader.getIcon(it.removeSuffix(".svg") + "_new.svg", Agents::class.java) }
            ?: neutralButtonIcon
    }

    private fun loadCustomIcon(path: String): Icon = try {
        val file = ideAgentTabsHome().resolve(Path.of(path))
        val icon = IconLoader.findIcon(file.toUri().toURL(), true)
        if (icon == null || icon.iconWidth <= 0) {
            LOG.warn("Could not load agent icon $file")
            neutralIcon
        } else {
            IconUtil.resizeSquared(icon, 16)
        }
    } catch (e: Exception) {
        LOG.warn("Could not load agent icon $path", e)
        neutralIcon
    }

    @RequiresEdt
    fun open(project: Project, profile: AgentProfile, via: LaunchVia? = null) {
        val directory = project.basePath ?: System.getProperty("user.home")
        val id = UUID.randomUUID().toString()
        val application = ApplicationManager.getApplication()
        application.executeOnPooledThread {
            if (project.isDisposed) return@executeOnPooledThread
            val planned = runCatching {
                val launch = launchFor(profile, cwd = directory, tabId = id, copilot = copilotFor(profile), via = via)
                launch to AgentTabLauncher.prepare(launch, id)
            }
            application.invokeLater({
                val (launch, shell) = planned.getOrElse {
                    if (it !is IllegalArgumentException) throw it
                    Messages.showErrorDialog(project, it.message, "Agent Tabs")
                    return@invokeLater
                }
                AgentTabLauncher.open(project, directory, profile, launch, shell, focus = true, id = id)
            }, project.disposed)
        }
    }
}
