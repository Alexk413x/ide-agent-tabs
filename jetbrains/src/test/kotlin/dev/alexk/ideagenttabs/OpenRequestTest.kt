package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
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
    fun `parses args and env`() {
        val path = dir.toString().replace("\\", "\\\\")
        val request = OpenRequest.parse("""{"path":"$path","args":["--plugin-dir","C:\\a b"],"env":{"FOO":"x y","EMPTY":""}}""")
        assertEquals(listOf("--plugin-dir", "C:\\a b"), request.args)
        assertEquals(mapOf("FOO" to "x y", "EMPTY" to ""), request.env)
    }

    @Test
    fun `parses an optional agent`() {
        val path = dir.toString().replace("\\", "\\\\")
        assertEquals("codex", OpenRequest.parse("""{"path":"$path","agent":"codex"}""").agent)
        assertNull(OpenRequest.parse("""{"path":"$path"}""").agent)
        assertNull(OpenRequest.parse("""{"path":"$path","agent":null}""").agent)
        for (bad in listOf(""""agent":1""", """"agent":"  """", """"agent":["codex"]""")) {
            val body = """{"path":"$path",$bad}"""
            assertThrows(body, IllegalArgumentException::class.java) { OpenRequest.parse(body) }
        }
    }

    @Test
    fun `args and env are optional`() {
        val request = OpenRequest.parse(body(dir.toString()))
        assertEquals(emptyList<String>(), request.args)
        assertEquals(emptyMap<String, String>(), request.env)
        val path = dir.toString().replace("\\", "\\\\")
        assertEquals(emptyList<String>(), OpenRequest.parse("""{"path":"$path","args":null,"env":null}""").args)
    }

    @Test
    fun `rejects bad args and env`() {
        val path = dir.toString().replace("\\", "\\\\")
        val bad = listOf(
            """"args":"--x"""",
            """"args":[1]""",
            """"args":[["x"]]""",
            """"env":["A"]""",
            """"env":{"A":1}""",
            """"env":{"A=B":"x"}""",
            """"env":{"A B":"x"}""",
            """"env":{"":"x"}""",
            """"env":{"$STARTUP_ENV":"x"}""",
            """"env":{"ide_agent_tabs_id":"x"}""",
            """"env":{"IDE_AGENT_TABS_ARG_0":"x"}""",
            """"env":{"JEDITERM_SOURCE_ARGS":"x"}""",
        )
        for (field in bad) {
            val body = """{"path":"$path",$field}"""
            assertThrows(body, IllegalArgumentException::class.java) { OpenRequest.parse(body) }
        }
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(dir.toString(), null, List(MAX_ENTRIES + 1) { "x" }) }
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(dir.toString(), null, listOf("x".repeat(MAX_PROMPT_CHARS + 1))) }
        assertThrows(IllegalArgumentException::class.java) { OpenRequest.of(dir.toString(), null, env = mapOf("A" to "x\u0000y")) }
    }

    @Test
    fun `close takes a string id`() {
        assertEquals("abc", parseCloseId("""{"id":"abc"}"""))
        for (bad in listOf("", "{}", """{"id":""}""", """{"id":"  "}""", """{"id":7}""", "[]", "nope")) {
            assertThrows(bad, IllegalArgumentException::class.java) { parseCloseId(bad) }
        }
    }

    @Test
    fun `input takes an id and one line of text`() {
        val line = "Agent Tabs: new message from codex 1a2b. Call read_messages."
        assertEquals(InputRequest("abc", line), parseInput("""{"id":"abc","text":"$line"}"""))
        val longest = "é".repeat(MAX_INPUT_CHARS)
        assertEquals(longest, parseInput("""{"id":"abc","text":"$longest"}""").text)
    }

    @Test
    fun `input rejects a missing id, missing text, long text and control characters`() {
        val bad = listOf(
            "", "[]", "nope", "{}",
            """{"text":"hi"}""",
            """{"id":"","text":"hi"}""",
            """{"id":"abc"}""",
            """{"id":"abc","text":""}""",
            """{"id":"abc","text":"  "}""",
            """{"id":"abc","text":7}""",
            """{"id":"abc","text":"${"x".repeat(MAX_INPUT_CHARS + 1)}"}""",
        ) + listOf("\\r", "\\n", "\\t", "\\u001b", "\\u0000", "\\u007f", "\\u009b").map { """{"id":"abc","text":"a${it}b"}""" }
        for (body in bad) {
            assertThrows(body, IllegalArgumentException::class.java) { parseInput(body) }
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
        val a = dir.resolve("work")
        val b = a.resolve("repo")
        val c = dir.resolve("other")
        assertEquals(1, closestBase(b.resolve("sub"), listOf(a, b, c)))
        assertEquals(0, closestBase(a.resolve("x"), listOf(a, b, null)))
        assertNull(closestBase(dir.resolve("none"), listOf(a, b, c)))
    }

    @Test
    fun `closest base ignores case on Windows`() {
        assumeTrue("Windows paths", File.separatorChar == '\\')
        assertEquals(1, closestBase(Path.of("c:\\WORK\\Repo"), listOf(Path.of("C:\\work"), Path.of("C:\\work\\repo"))))
    }
}
