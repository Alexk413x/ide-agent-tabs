package dev.alexk.ideagenttabs

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.IconLoader
import com.intellij.openapi.util.SystemInfo
import com.intellij.util.EnvironmentUtil
import com.intellij.util.IconUtil
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import javax.swing.Icon

object Agents {

    private val LOG = logger<Agents>()

    val settings = AgentSettings(ideAgentTabsHome()) { LOG.warn(it) }

    private val neutralIcon: Icon = IconLoader.getIcon("/icons/agentTab.svg", Agents::class.java)

    private val builtinIcons = mapOf(
        "claude" to "/icons/agents/claude.svg",
        "codex" to "/icons/agents/codex.svg",
        "gemini" to "/icons/agents/gemini.svg",
        "copilot" to "/icons/agents/copilot.svg",
    )

    private val customIcons = ConcurrentHashMap<String, Icon>()

    private val searchPath: String
        get() = EnvironmentUtil.getValue("PATH") ?: System.getenv("PATH").orEmpty()

    fun isInstalled(profile: AgentProfile): Boolean = isInstalled(profile.command, searchPath, SystemInfo.isWindows)

    fun installedProfiles(): List<AgentProfile> {
        val path = searchPath
        return settings.profiles().filter { isInstalled(it.command, path, SystemInfo.isWindows) }
    }

    fun icon(profile: AgentProfile): Icon {
        profile.icon?.let { return customIcons.computeIfAbsent(it, ::loadCustomIcon) }
        return builtinIcons[profile.name]?.let { IconLoader.getIcon(it, Agents::class.java) } ?: neutralIcon
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
    fun open(project: Project, profile: AgentProfile, remember: Boolean) {
        if (remember) settings.setDefaultAgent(profile.name)
        val directory = project.basePath ?: System.getProperty("user.home")
        AgentTabLauncher.open(project, directory, profile, prompt = null, focus = true)
    }
}
