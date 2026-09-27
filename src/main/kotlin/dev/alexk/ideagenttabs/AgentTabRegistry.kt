package dev.alexk.ideagenttabs

import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerListener
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import java.util.concurrent.ConcurrentHashMap

@Service(Service.Level.APP)
class AgentTabRegistry {

    class Entry(val id: String, val agent: String, val project: Project, val file: VirtualFile, val path: String)

    private val entries = ConcurrentHashMap<String, Entry>()

    fun add(entry: Entry) {
        entries[entry.id] = entry
    }

    fun remove(id: String): Entry? = entries.remove(id)

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

class AgentTabClosedListener : FileEditorManagerListener {
    override fun fileClosed(source: FileEditorManager, file: VirtualFile) {
        AgentTabRegistry.getInstance().removeFile(file)
    }
}
