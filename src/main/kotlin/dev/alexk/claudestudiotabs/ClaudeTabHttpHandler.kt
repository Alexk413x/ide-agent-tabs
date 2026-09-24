package dev.alexk.claudestudiotabs

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

const val ENDPOINT_PATH = "/claude-studio-tabs/open"
const val START_TIMEOUT_SECONDS = 10L

class ClaudeTabHttpHandler : HttpRequestHandler() {

    override fun isSupported(request: FullHttpRequest): Boolean =
        QueryStringDecoder(request.uri()).path() == ENDPOINT_PATH

    override fun process(urlDecoder: QueryStringDecoder, request: FullHttpRequest, context: ChannelHandlerContext): Boolean {
        val remote = (context.channel().remoteAddress() as? InetSocketAddress)?.address
        Admission.check(remote, request.method().name()) { request.headers().get(it) }?.let {
            respond(context, it.status, error(it.message))
            return true
        }
        val open = try {
            OpenRequest.parse(request.content().toString(Charsets.UTF_8))
        } catch (e: IllegalArgumentException) {
            respond(context, 400, error(e.message ?: "bad request"))
            return true
        }
        val claimed = AtomicBoolean(false)
        AppExecutorUtil.getAppScheduledExecutorService().schedule({
            if (claimed.compareAndSet(false, true)) {
                respond(context, 503, error("the IDE did not respond in $START_TIMEOUT_SECONDS s, likely a modal dialog; nothing was opened"))
            }
        }, START_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        ApplicationManager.getApplication().invokeLater {
            if (!claimed.compareAndSet(false, true)) return@invokeLater
            val project = chooseProject(open.path)
            if (project == null) {
                respond(context, 409, error("no open project to host the tab"))
                return@invokeLater
            }
            try {
                ClaudeTabLauncher.open(project, open.path.toString(), open.prompt, focus = false)
                respond(context, 200, JsonObject().apply {
                    addProperty("ok", true)
                    addProperty("project", project.name)
                    addProperty("path", open.path.toString())
                })
            } catch (e: Exception) {
                LOG.warn("Opening a Claude tab failed", e)
                respond(context, 500, error(e.toString()))
            }
        }
        return true
    }

    private fun chooseProject(path: Path): Project? {
        val projects = ProjectManager.getInstance().openProjects.filter { !it.isDisposed && !it.isDefault }
        closestBase(path, projects.map { p -> p.basePath?.let { Path.of(it) } })?.let { return projects[it] }
        return IdeFocusManager.getGlobalInstance().lastFocusedFrame?.project?.takeIf { it in projects }
            ?: projects.firstOrNull()
    }

    private fun error(message: String) = JsonObject().apply {
        addProperty("ok", false)
        addProperty("error", message)
    }

    private fun respond(context: ChannelHandlerContext, status: Int, body: JsonObject) {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        val response = DefaultFullHttpResponse(HttpVersion.HTTP_1_1, HttpResponseStatus.valueOf(status), Unpooled.wrappedBuffer(bytes))
        response.headers()
            .set(HttpHeaderNames.CONTENT_TYPE, "application/json; charset=utf-8")
            .set(HttpHeaderNames.CONTENT_LENGTH, bytes.size)
        context.channel().writeAndFlush(response).addListener(ChannelFutureListener.CLOSE)
    }

    private companion object {
        val LOG = logger<ClaudeTabHttpHandler>()
    }
}
