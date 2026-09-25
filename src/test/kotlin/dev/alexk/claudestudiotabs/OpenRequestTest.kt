package dev.alexk.claudestudiotabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path

class OpenRequestTest {

    private val dir: Path = Files.createTempDirectory("cst")

    private fun body(path: String, prompt: String? = null): String {
        val escaped = path.replace("\\", "\\\\")
        return if (prompt == null) """{"path":"$escaped"}""" else """{"path":"$escaped","prompt":"$prompt"}"""
    }

    @Test
    fun `parses path and prompt`() {
        val request = OpenRequest.parse(body(dir.toString(), "hello"))
        assertEquals(dir, request.path)
        assertEquals("hello", request.prompt)
    }

    @Test
    fun `blank prompt means no prompt`() {
        assertNull(OpenRequest.parse(body(dir.toString())).prompt)
        assertNull(OpenRequest.parse(body(dir.toString(), "  ")).prompt)
    }

    @Test
    fun `rejects bad bodies`() {
        for (bad in listOf("", "not json", "[]", """{"prompt":"x"}""", """{"path":1}""", """{"path":"${dir.toString().replace("\\", "\\\\")}","prompt":{}}""")) {
            assertThrows(bad, IllegalArgumentException::class.java) { OpenRequest.parse(bad) }
        }
    }

    @Test
    fun `rejects relative and missing directories`() {
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of("relative\\dir", null) }
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(dir.resolve("absent").toString(), null) }
        val file = Files.createTempFile(dir, "f", ".txt")
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(file.toString(), null) }
    }

    @Test
    fun `rejects an oversized prompt`() {
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(dir.toString(), "x".repeat(MAX_PROMPT_CHARS + 1)) }
        assertEquals(MAX_PROMPT_CHARS, OpenRequest.of(dir.toString(), "x".repeat(MAX_PROMPT_CHARS)).prompt?.length)
    }

    @Test
    fun `close takes a string id`() {
        assertEquals("abc", parseCloseId("""{"id":"abc"}"""))
        for (bad in listOf("", "{}", """{"id":""}""", """{"id":"  "}""", """{"id":7}""", "[]", "nope")) {
            assertThrows(bad, IllegalArgumentException::class.java) { parseCloseId(bad) }
        }
    }

    @Test
    fun `list accepts an empty body or an object`() {
        parseEmpty("")
        parseEmpty("{}")
        assertThrows(IllegalArgumentException::class.java) { parseEmpty("[]") }
        assertThrows(IllegalArgumentException::class.java) { parseEmpty("nope") }
    }

    @Test
    fun `closest base picks the deepest containing project`() {
        val a = Path.of("C:\\work")
        val b = Path.of("C:\\work\\repo")
        val c = Path.of("D:\\other")
        assertEquals(1, closestBase(Path.of("C:\\work\\repo\\sub"), listOf(a, b, c)))
        assertEquals(0, closestBase(Path.of("C:\\work\\x"), listOf(a, b, null)))
        assertEquals(1, closestBase(Path.of("c:\\WORK\\Repo"), listOf(a, b)))
        assertNull(closestBase(Path.of("E:\\none"), listOf(a, b, c)))
    }
}
