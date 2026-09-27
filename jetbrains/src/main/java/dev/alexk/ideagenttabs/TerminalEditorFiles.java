package dev.alexk.ideagenttabs;

import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.terminal.frontend.editor.TerminalViewVirtualFile;
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTab;

// Java, not Kotlin: TerminalViewVirtualFile is Kotlin-internal, so Kotlin callers cannot name it,
// but its bytecode is public. The platform's own Move to Editor action forces focus, so it is not an option.
final class TerminalEditorFiles {

    private TerminalEditorFiles() {
    }

    static VirtualFile of(TerminalToolWindowTab tab) {
        return new TerminalViewVirtualFile(tab);
    }
}
