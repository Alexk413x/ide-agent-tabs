package dev.alexk.ideagenttabs

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ApplicationNamesInfo
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.ProjectManager
import com.intellij.openapi.wm.IdeFocusManager
import com.intellij.util.concurrency.AppExecutorUtil
import io.netty.buffer.Unpooled
import io.netty.channel.ChannelFutureListener
import io.netty.channel.ChannelHandlerContext
import io.netty.handler.codec.http.DefaultFullHttpResponse
import io.netty.handler.codec.http.FullHttpRequest
import io.netty.handler.codec.http.HttpHeaderNames
import io.netty.handler.codec.http.HttpResponseStatus
import io.netty.handler.codec.http.HttpVersion
import io.netty.handler.codec.http.QueryStringDecoder
import org.jetbrains.ide.HttpRequestHandler
import java.net.InetSocketAddress
import java.nio.file.Path
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

const val ENDPOINT_BASE = "/ide-agent-tabs"
const val START_TIMEOUT_SECONDS = 10L

private val ROUTES = setOf("info", "agents", "open", "close", "list", "input")

private class Reply(val status: Int, val body: JsonObject)

class AgentTabHttpHandler : HttpRequestHandler() {

    override fun isSupported(request: FullHttpRequest): Boolean = route(QueryStringDecoder(request.uri()).path()) != null

    private fun route(path: String): String? = path.removePrefix("$ENDPOINT_BASE/").takeIf { it != path && it in ROUTES }

    override fun process(urlDecoder: QueryStringDecoder, request: FullHttpRequest, context: ChannelHandlerContext): Boolean {
        val remote = (context.channel().remoteAddress() as? InetSocketAddress)?.address
        Admission.check(remote, request.method().name(), service<EndpointFile>().token) { request.headers().get(it) }?.let {
            respond(context, Reply(it.status, error(it.message)))
            return true
        }
        val body = request.content().toString(Charsets.UTF_8)
        try {
            when (route(urlDecoder.path())) {
                "open" -> {
                    val open = OpenRequest.parse(body)
                    val profile = open.agent?.let {
                        Agents.settings.profile(it) ?: throw IllegalArgumentException("unknown agent: $it")
                    } ?: Agents.settings.defaultProfile()
                    val launch = Agents.launchFor(profile, open)
                    onEdt(context) { open(open, profile, launch) }
                }
                "close" -> parseCloseId(body).let { onEdt(context) { close(it) } }
                "input" -> parseInput(body).let { onEdt(context) { input(it) } }
                "agents" -> {
                    parseEmpty(body)
                    AppExecutorUtil.getAppExecutorService().execute { respond(context, attempt(::agents)) }
                }
                "info" -> {
                    parseEmpty(body)
                    onEdt(context) { info() }
                }
                else -> {
                    parseEmpty(body)
                    onEdt(context) { list() }
                }
            }
        } catch (e: IllegalArgumentException) {
            respond(context, Reply(400, error(e.message ?: "bad request")))
        }
        return true
    }

    private fun onEdt(context: ChannelHandlerContext, work: () -> Reply) {
        val claimed = AtomicBoolean(false)
        AppExecutorUtil.getAppScheduledExecutorService().schedule({
            if (claimed.compareAndSet(false, true)) {
                respond(context, Reply(503, error("the IDE did not respond in $START_TIMEOUT_SECONDS s, likely a modal dialog; nothing was done")))
            }
        }, START_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        ApplicationManager.getApplication().invokeLater {
            if (!claimed.compareAndSet(false, true)) return@invokeLater
            respond(context, attempt(work))
        }
    }

    private fun attempt(work: () -> Reply): Reply = try {
        work()
    } catch (e: Exception) {
        LOG.warn("Agent tab request failed", e)
        Reply(500, error(e.toString()))
    }

    private fun open(request: OpenRequest, profile: AgentProfile, launch: AgentLaunch): Reply {
        val project = chooseProject(request.path) ?: return Reply(409, error("no open project to host the tab"))
        val id = AgentTabLauncher.open(project, request.path.toString(), profile, launch, focus = false)
        return Reply(200, ok().apply {
            addProperty("id", id)
            addProperty("agent", profile.name)
            addProperty("project", project.name)
            addProperty("path", request.path.toString())
            addProperty("via", launch.via.value)
        })
    }

    private fun close(id: String): Reply {
        val entry = AgentTabRegistry.getInstance().remove(id)
            ?: return Reply(404, error("no open agent tab with id $id; only tabs this plugin opened can be closed"))
        AgentTabLauncher.close(entry)
        return Reply(200, ok().apply { addProperty("id", id) })
    }

    private fun input(request: InputRequest): Reply {
        val entry = AgentTabRegistry.getInstance().find(request.id)
            ?: return Reply(404, error("no open agent tab with id ${request.id}; only tabs this plugin opened take input"))
        AgentTabLauncher.type(entry, request.text)
        return Reply(200, ok().apply { addProperty("id", request.id) })
    }

    private fun list(): Reply {
        val tabs = JsonArray()
        for (entry in AgentTabRegistry.getInstance().live()) {
            tabs.add(JsonObject().apply {
                addProperty("id", entry.id)
                addProperty("agent", entry.agent)
                addProperty("project", entry.project.name)
                addProperty("path", entry.path)
            })
        }
        return Reply(200, ok().apply { add("tabs", tabs) })
    }

    private fun info(): Reply {
        val focused = lastFocusedProject()
        val projects = JsonArray()
        for (project in openProjects()) {
            val path = project.basePath ?: continue
            projects.add(JsonObject().apply {
                addProperty("name", project.name)
                addProperty("path", Path.of(path).toString())
                addProperty("focused", project == focused)
            })
        }
        return Reply(200, ok().apply {
            addProperty("ide", "jetbrains")
            addProperty("product", ApplicationNamesInfo.getInstance().fullProductName)
            addProperty("version", ApplicationInfo.getInstance().fullVersion)
            addProperty("pid", ProcessHandle.current().pid())
            add("projects", projects)
        })
    }

    private fun agents(): Reply {
        val agents = JsonArray()
        for (profile in Agents.settings.profiles()) {
            agents.add(JsonObject().apply {
                addProperty("name", profile.name)
                addProperty("label", profile.label)
                addProperty("command", profile.command)
                addProperty("installed", Agents.isInstalled(profile))
            })
        }
        return Reply(200, ok().apply {
            addProperty("default", Agents.settings.defaultProfile().name)
            add("agents", agents)
        })
    }

    private fun openProjects(): List<Project> =
        ProjectManager.getInstance().openProjects.filter { !it.isDisposed && !it.isDefault }

    private fun lastFocusedProject(): Project? = IdeFocusManager.getGlobalInstance().lastFocusedFrame?.project

    private fun chooseProject(path: Path): Project? {
        val projects = openProjects()
        closestBase(path, projects.map { p -> p.basePath?.let { Path.of(it) } })?.let { return projects[it] }
        return lastFocusedProject()?.takeIf { it in projects } ?: projects.firstOrNull()
    }

    private fun ok() = JsonObject().apply { addProperty("ok", true) }

    private fun error(message: String) = JsonObject().apply {
        addProperty("ok", false)
        addProperty("error", message)
    }

    private fun respond(context: ChannelHandlerContext, reply: Reply) {
        val bytes = reply.body.toString().toByteArray(Charsets.UTF_8)
        val response = DefaultFullHttpResponse(HttpVersion.HTTP_1_1, HttpResponseStatus.valueOf(reply.status), Unpooled.wrappedBuffer(bytes))
        response.headers()
            .set(HttpHeaderNames.CONTENT_TYPE, "application/json; charset=utf-8")
            .set(HttpHeaderNames.CONTENT_LENGTH, bytes.size)
        context.channel().writeAndFlush(response).addListener(ChannelFutureListener.CLOSE)
    }

    private companion object {
        val LOG = logger<AgentTabHttpHandler>()
    }
}
