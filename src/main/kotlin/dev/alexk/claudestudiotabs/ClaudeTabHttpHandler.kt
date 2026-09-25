package dev.alexk.claudestudiotabs

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.intellij.openapi.application.ApplicationManager
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

const val ENDPOINT_BASE = "/claude-studio-tabs"
const val OPEN_PATH = "$ENDPOINT_BASE/open"
const val CLOSE_PATH = "$ENDPOINT_BASE/close"
const val LIST_PATH = "$ENDPOINT_BASE/list"
const val START_TIMEOUT_SECONDS = 10L

private class Reply(val status: Int, val body: JsonObject)

class ClaudeTabHttpHandler : HttpRequestHandler() {

    override fun isSupported(request: FullHttpRequest): Boolean =
        QueryStringDecoder(request.uri()).path() in setOf(OPEN_PATH, CLOSE_PATH, LIST_PATH)

    override fun process(urlDecoder: QueryStringDecoder, request: FullHttpRequest, context: ChannelHandlerContext): Boolean {
        val remote = (context.channel().remoteAddress() as? InetSocketAddress)?.address
        Admission.check(remote, request.method().name()) { request.headers().get(it) }?.let {
            respond(context, Reply(it.status, error(it.message)))
            return true
        }
        val body = request.content().toString(Charsets.UTF_8)
        val work: () -> Reply = try {
            when (urlDecoder.path()) {
                OPEN_PATH -> OpenRequest.parse(body).let { { open(it) } }
                CLOSE_PATH -> parseCloseId(body).let { { close(it) } }
                else -> parseEmpty(body).let { { list() } }
            }
        } catch (e: IllegalArgumentException) {
            respond(context, Reply(400, error(e.message ?: "bad request")))
            return true
        }
        onEdt(context, work)
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
            val reply = try {
                work()
            } catch (e: Exception) {
                LOG.warn("Claude tab request failed", e)
                Reply(500, error(e.toString()))
            }
            respond(context, reply)
        }
    }

    private fun open(request: OpenRequest): Reply {
        val project = chooseProject(request.path) ?: return Reply(409, error("no open project to host the tab"))
        val id = ClaudeTabLauncher.open(project, request.path.toString(), request.prompt, focus = false, request.args, request.env)
        return Reply(200, ok().apply {
            addProperty("id", id)
            addProperty("project", project.name)
            addProperty("path", request.path.toString())
        })
    }

    private fun close(id: String): Reply {
        val entry = ClaudeTabRegistry.getInstance().remove(id)
            ?: return Reply(404, error("no open Claude tab with id $id; only tabs this plugin opened can be closed"))
        ClaudeTabLauncher.close(entry)
        return Reply(200, ok().apply { addProperty("id", id) })
    }

    private fun list(): Reply {
        val tabs = JsonArray()
        for (entry in ClaudeTabRegistry.getInstance().live()) {
            tabs.add(JsonObject().apply {
                addProperty("id", entry.id)
                addProperty("project", entry.project.name)
                addProperty("path", entry.path)
            })
        }
        return Reply(200, ok().apply { add("tabs", tabs) })
    }

    private fun chooseProject(path: Path): Project? {
        val projects = ProjectManager.getInstance().openProjects.filter { !it.isDisposed && !it.isDefault }
        closestBase(path, projects.map { p -> p.basePath?.let { Path.of(it) } })?.let { return projects[it] }
        return IdeFocusManager.getGlobalInstance().lastFocusedFrame?.project?.takeIf { it in projects }
            ?: projects.firstOrNull()
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
        val LOG = logger<ClaudeTabHttpHandler>()
    }
}
