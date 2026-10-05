package dev.alexk.ideagenttabs

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.IconLoader
import com.intellij.openapi.util.SystemInfo
import com.intellij.ui.LayeredIcon
import com.intellij.util.EnvironmentUtil
import com.intellij.util.IconUtil
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import javax.swing.Icon

const val VIA_LABEL = "via OpenRouter"

fun viaLabel(text: String, via: LaunchVia) = if (via == LaunchVia.ORI) "$text ($VIA_LABEL)" else text

object Agents {

    private val LOG = logger<Agents>()

    val settings = AgentSettings(ideAgentTabsHome()) { LOG.warn(it) }

    private val neutralIcon: Icon = IconLoader.getIcon("/icons/agentTab.svg", Agents::class.java)

    private val builtinIcons = mapOf(
        "claude" to "/icons/agents/claude.svg",
        "codex" to "/icons/agents/codex.svg",
        "gemini" to "/icons/agents/gemini.svg",
        "copilot" to "/icons/agents/copilot.svg",
        "agy" to "/icons/agents/agy.svg",
        "grok" to "/icons/agents/grok.svg",
        "pi" to "/icons/agents/pi.svg",
        "hermes" to "/icons/agents/hermes.svg",
        "opencode" to "/icons/agents/opencode.svg",
        "qwen" to "/icons/agents/qwen.svg",
        "goose" to "/icons/agents/goose.svg",
        "codex-local" to "/icons/agents/codex.svg",
    )

    private val neutralButtonIcon: Icon = IconLoader.getIcon("/icons/agentTab_new.svg", Agents::class.java)

    val viaIcon: Icon = IconLoader.getIcon("/icons/openrouter.svg", Agents::class.java)

    private val badge: Icon = IconLoader.getIcon("/icons/newBadge.svg", Agents::class.java)

    private val customIcons = ConcurrentHashMap<String, Icon>()

    private val customButtonIcons = ConcurrentHashMap<String, Icon>()

    private val searchPath: String
        get() = EnvironmentUtil.getValue("PATH") ?: System.getenv("PATH").orEmpty()

    fun launchFor(profile: AgentProfile, request: OpenRequest? = null): AgentLaunch =
        planLaunch(
            profile,
            LaunchContext(
                prompt = request?.prompt,
                args = request?.args.orEmpty(),
                env = request?.env.orEmpty(),
                model = request?.model,
                via = request?.via,
                setting = settings.shared().launchVia,
                ori = settings.detected().ori,
                windows = SystemInfo.isWindows,
                searchPath = searchPath,
            ),
        )

    fun isInstalled(profile: AgentProfile): Boolean = isInstalled(profile.command, searchPath, SystemInfo.isWindows)

    fun installedProfiles(): List<AgentProfile> {
        val path = searchPath
        return settings.profiles().filter { isInstalled(it.command, path, SystemInfo.isWindows) }
    }

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

    fun setDefaultAgent(profile: AgentProfile): Boolean = settings.setDefaultAgent(profile.name)

    @RequiresEdt
    fun open(project: Project, profile: AgentProfile) {
        val directory = project.basePath ?: System.getProperty("user.home")
        AgentTabLauncher.open(project, directory, profile, launchFor(profile), focus = true)
    }
}
