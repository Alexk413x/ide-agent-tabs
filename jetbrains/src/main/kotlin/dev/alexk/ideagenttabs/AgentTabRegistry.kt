package dev.alexk.ideagenttabs

import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerListener
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTab
import com.intellij.util.concurrency.annotations.RequiresEdt
import java.util.concurrent.ConcurrentHashMap

@Service(Service.Level.APP)
class AgentTabRegistry {

    class Entry(
        val id: String,
        val agent: String,
        val project: Project,
        val file: VirtualFile,
        val path: String,
        val tab: TerminalToolWindowTab,
    )

    private val entries = ConcurrentHashMap<String, Entry>()

    fun add(entry: Entry) {
        entries[entry.id] = entry
    }

    fun remove(id: String): Entry? = entries.remove(id)

    fun find(id: String): Entry? = entries[id]?.takeUnless { it.project.isDisposed }

    // The terminal plugin owns the editor tabs, so they outlive this plugin's unload and reload while the map does not.
    @RequiresEdt
    fun revive(project: Project): Int {
        var count = 0
        for (file in FileEditorManager.getInstance(project).openFiles) {
            val tab = TerminalEditorFiles.tabOf(file) ?: continue
            val found = revivedTab(tab.processOptions.envVariables, tab.processOptions.workingDirectory) ?: continue
            if (entries.values.any { it.file == file }) continue
            if (entries.putIfAbsent(found.id, Entry(found.id, found.agent, project, file, found.path, tab)) == null) count++
        }
        return count
    }

    fun removeFile(file: VirtualFile) {
        entries.values.removeIf { it.file == file }
    }

    fun live(): List<Entry> {
        entries.values.removeIf { it.project.isDisposed }
        return entries.values.toList()
    }

    companion object {
        fun getInstance(): AgentTabRegistry = service()
    }
}

class RevivedTab(val id: String, val agent: String, val path: String)

fun revivedTab(env: Map<String, String>, workingDirectory: String?): RevivedTab? {
    val id = env[TAB_ID_ENV]?.takeIf { it.isNotEmpty() } ?: return null
    return RevivedTab(id, env[AGENT_ENV].orEmpty(), workingDirectory.orEmpty())
}

class AgentTabClosedListener : FileEditorManagerListener {
    override fun fileClosed(source: FileEditorManager, file: VirtualFile) {
        AgentTabRegistry.getInstance().removeFile(file)
    }
}
