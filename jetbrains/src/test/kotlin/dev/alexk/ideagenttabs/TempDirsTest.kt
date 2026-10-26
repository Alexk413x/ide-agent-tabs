package dev.alexk.ideagenttabs

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.Description
import org.junit.runners.model.Statement
import java.nio.file.Files
import java.nio.file.Path

class TempDirsTest {

    @Test
    fun `the rule removes every created directory with its contents`() {
        val temp = TempDirs()
        val created = mutableListOf<Path>()
        temp.apply(object : Statement() {
            override fun evaluate() {
                created.add(temp.create("cst-a"))
                val second = temp.create("cst-b")
                created.add(second)
                Files.writeString(Files.createDirectory(second.resolve("nested")).resolve("f.txt"), "x")
                assertTrue(created.all { Files.exists(it) })
            }
        }, Description.EMPTY).evaluate()

        assertFalse(created.any { Files.exists(it) })
    }
}
