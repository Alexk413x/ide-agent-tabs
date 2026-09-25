package dev.alexk.claudestudiotabs

import com.google.gson.JsonObject
import com.intellij.openapi.Disposable
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import org.jetbrains.ide.BuiltInServerManager
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption

val ENDPOINT_FILE: Path = Path.of(System.getProperty("user.home"), ".claude-studio-tabs", "endpoint.json")

@Service(Service.Level.APP)
class EndpointFile : Disposable {

    private val pid = ProcessHandle.current().pid()

    @Volatile
    private var written = false

    fun write() {
        if (written) return
        val port = BuiltInServerManager.getInstance().waitForStart().port
        val body = JsonObject().apply {
            addProperty("url", "http://127.0.0.1:$port$OPEN_PATH")
            addProperty("close", "http://127.0.0.1:$port$CLOSE_PATH")
            addProperty("list", "http://127.0.0.1:$port$LIST_PATH")
            addProperty("port", port)
            addProperty("pid", pid)
        }
        try {
            Files.createDirectories(ENDPOINT_FILE.parent)
            val temp = Files.createTempFile(ENDPOINT_FILE.parent, "endpoint", ".tmp")
            Files.writeString(temp, body.toString())
            Files.move(temp, ENDPOINT_FILE, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
            written = true
        } catch (e: Exception) {
            LOG.warn("Could not write $ENDPOINT_FILE", e)
        }
    }

    override fun dispose() {
        if (!written) return
        try {
            if (Files.readString(ENDPOINT_FILE).contains("\"pid\":$pid")) Files.delete(ENDPOINT_FILE)
        } catch (_: Exception) {
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
