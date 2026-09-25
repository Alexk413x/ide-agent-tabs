package dev.alexk.claudestudiotabs

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import java.net.InetAddress
import java.nio.file.Files
import java.nio.file.Path

const val MAX_PROMPT_CHARS = 30_000

class Refusal(val status: Int, val message: String)

// Pure module: no IDE or Netty types, so the admission rules test without a running IDE.
object Admission {

    fun check(remote: InetAddress?, method: String, header: (String) -> String?): Refusal? = when {
        remote == null || !remote.isLoopbackAddress -> Refusal(403, "loopback requests only")
        method != "POST" -> Refusal(405, "use POST")
        header("Origin") != null || header("Referer") != null -> Refusal(403, "browser requests are refused")
        header("Content-Type")?.substringBefore(';')?.trim()?.lowercase() != "application/json" ->
            Refusal(415, "Content-Type must be application/json")
        else -> null
    }
}

private fun parseObject(body: String): JsonObject {
    val json = try {
        JsonParser.parseString(body)
    } catch (e: JsonSyntaxException) {
        throw IllegalArgumentException("body is not JSON")
    }
    if (!json.isJsonObject) throw IllegalArgumentException("body must be a JSON object")
    return json.asJsonObject
}

private fun JsonObject.string(name: String): String? {
    val value = get(name) ?: return null
    if (value.isJsonNull) return null
    if (!value.isJsonPrimitive || !value.asJsonPrimitive.isString) {
        throw IllegalArgumentException("$name must be a string")
    }
    return value.asString
}

fun parseCloseId(body: String): String {
    val id = parseObject(body).string("id")
    if (id.isNullOrBlank()) throw IllegalArgumentException("id is required")
    return id
}

fun parseEmpty(body: String) {
    if (body.isNotBlank()) parseObject(body)
}

data class OpenRequest(val path: Path, val prompt: String?) {

    companion object {

        fun parse(body: String): OpenRequest {
            val obj = parseObject(body)
            return of(obj.string("path"), obj.string("prompt"))
        }

        fun of(path: String?, prompt: String?): OpenRequest {
            if (path.isNullOrBlank()) throw IllegalArgumentException("path is required")
            val dir = Path.of(path)
            if (!dir.isAbsolute) throw IllegalArgumentException("path must be absolute")
            if (!Files.isDirectory(dir)) throw IllegalArgumentException("path is not a directory: $path")
            if (prompt != null && prompt.length > MAX_PROMPT_CHARS) {
                throw IllegalArgumentException("prompt exceeds $MAX_PROMPT_CHARS characters")
            }
            return OpenRequest(dir.normalize(), prompt?.takeIf { it.isNotBlank() })
        }
    }
}

fun closestBase(target: Path, bases: List<Path?>): Int? =
    bases.withIndex()
        .filter { (_, base) -> base != null && target.startsWith(base) }
        .maxByOrNull { (_, base) -> base!!.nameCount }
        ?.index
