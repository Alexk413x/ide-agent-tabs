package dev.alexk.ideagenttabs

import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.application.ApplicationNamesInfo
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.util.concurrency.AppExecutorUtil
import org.jetbrains.ide.BuiltInServerManager
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

@Service(Service.Level.APP)
class EndpointFile : Disposable {

    val token: String = newToken()

    private val pid = ProcessHandle.current().pid()

    @Volatile
    private var file: Path? = null

    private var beat: ScheduledFuture<*>? = null

    @Synchronized
    fun write() {
        if (file != null) return
        val port = BuiltInServerManager.getInstance().waitForStart().port
        val entry = endpointJson(
            product = ApplicationNamesInfo.getInstance().fullProductName,
            version = ApplicationInfo.getInstance().fullVersion,
            pid = pid,
            url = "http://127.0.0.1:$port$ENDPOINT_BASE",
            token = token,
            startedAt = System.currentTimeMillis(),
        ).toString()
        val target = ideAgentTabsHome().resolve("endpoints").resolve("jetbrains-$pid.json")
        file = target
        try {
            writeAtomically(target, entry, private = true)
        } catch (e: Exception) {
            LOG.warn("Could not write $target", e)
        }
        beat = AppExecutorUtil.getAppScheduledExecutorService().scheduleWithFixedDelay({
            try {
                beatEndpoint(target, entry)
            } catch (e: Exception) {
                LOG.warn("Could not refresh $target", e)
            }
        }, ENDPOINT_BEAT_MS, ENDPOINT_BEAT_MS, TimeUnit.MILLISECONDS)
    }

    @Synchronized
    override fun dispose() {
        beat?.cancel(false)
        val written = file ?: return
        try {
            Files.deleteIfExists(written)
        } catch (e: Exception) {
            LOG.warn("Could not delete $written", e)
        }
    }

    private companion object {
        val LOG = logger<EndpointFile>()
    }
}

class EndpointFileActivity : ProjectActivity {
    override suspend fun execute(project: Project) {
        service<EndpointFile>().write()
    }
}
