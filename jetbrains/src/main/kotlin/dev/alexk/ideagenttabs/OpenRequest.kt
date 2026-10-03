package dev.alexk.ideagenttabs

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import java.net.InetAddress
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest

const val MAX_PROMPT_CHARS = 30_000
const val MAX_ENTRIES = 64
const val MAX_INPUT_CHARS = 500

val MODEL_PATTERN = Regex("[A-Za-z0-9._:/@+-]{1,200}")

class Refusal(val status: Int, val message: String)

// Pure module: no IDE or Netty types, so the admission rules test without a running IDE.
object Admission {

    fun check(remote: InetAddress?, method: String, token: String, header: (String) -> String?): Refusal? = when {
        remote == null || !remote.isLoopbackAddress -> Refusal(403, "loopback requests only")
        method != "POST" -> Refusal(405, "use POST")
        header("Origin") != null || header("Referer") != null -> Refusal(403, "browser requests are refused")
        !bearerMatches(header("Authorization"), token) -> Refusal(401, "missing or wrong token; send Authorization: Bearer <token>")
        header("Content-Type")?.substringBefore(';')?.trim()?.lowercase() != "application/json" ->
            Refusal(415, "Content-Type must be application/json")
        else -> null
    }

    fun bearerMatches(authorization: String?, token: String): Boolean {
        if (token.isEmpty()) return false
        val parts = authorization?.trim()?.split(' ', limit = 2) ?: return false
        if (parts.size != 2 || !parts[0].equals("Bearer", ignoreCase = true)) return false
        return MessageDigest.isEqual(parts[1].trim().toByteArray(Charsets.UTF_8), token.toByteArray(Charsets.UTF_8))
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

data class InputRequest(val id: String, val text: String)

fun parseInput(body: String): InputRequest {
    val obj = parseObject(body)
    val id = obj.string("id")
    if (id.isNullOrBlank()) throw IllegalArgumentException("id is required")
    val text = obj.string("text")
    if (text.isNullOrBlank()) throw IllegalArgumentException("text is required")
    if (text.length > MAX_INPUT_CHARS) throw IllegalArgumentException("text exceeds $MAX_INPUT_CHARS characters")
    if (text.any { Character.getType(it) == Character.CONTROL.toInt() }) {
        throw IllegalArgumentException("text must be one line with no control characters")
    }
    return InputRequest(id, text)
}

fun parseEmpty(body: String) {
    if (body.isNotBlank()) parseObject(body)
}

private fun JsonObject.stringList(name: String): List<String> {
    val value = get(name) ?: return emptyList()
    if (value.isJsonNull) return emptyList()
    if (!value.isJsonArray) throw IllegalArgumentException("$name must be an array of strings")
    return value.asJsonArray.map {
        if (!it.isJsonPrimitive || !it.asJsonPrimitive.isString) throw IllegalArgumentException("$name must be an array of strings")
        it.asString
    }
}

private fun JsonObject.stringMap(name: String): Map<String, String> {
    val value = get(name) ?: return emptyMap()
    if (value.isJsonNull) return emptyMap()
    if (!value.isJsonObject) throw IllegalArgumentException("$name must be an object of strings")
    return value.asJsonObject.entrySet().associate { (key, v) ->
        if (!v.isJsonPrimitive || !v.asJsonPrimitive.isString) throw IllegalArgumentException("$name.$key must be a string")
        key to v.asString
    }
}

data class OpenRequest(
    val path: Path,
    val prompt: String?,
    val args: List<String> = emptyList(),
    val env: Map<String, String> = emptyMap(),
    val agent: String? = null,
    val model: String? = null,
    val via: LaunchVia? = null,
) {

    companion object {

        fun parse(body: String): OpenRequest {
            val obj = parseObject(body)
            return of(
                obj.string("path"),
                obj.string("prompt"),
                obj.stringList("args"),
                obj.stringMap("env"),
                obj.string("agent"),
                obj.string("model"),
                obj.string("via"),
            )
        }

        fun of(
            path: String?,
            prompt: String?,
            args: List<String> = emptyList(),
            env: Map<String, String> = emptyMap(),
            agent: String? = null,
            model: String? = null,
            via: String? = null,
        ): OpenRequest {
            if (path.isNullOrBlank()) throw IllegalArgumentException("path is required")
            val dir = Path.of(path)
            if (!dir.isAbsolute) throw IllegalArgumentException("path must be absolute")
            if (!Files.isDirectory(dir)) throw IllegalArgumentException("path is not a directory: $path")
            if (prompt != null && prompt.length > MAX_PROMPT_CHARS) {
                throw IllegalArgumentException("prompt exceeds $MAX_PROMPT_CHARS characters")
            }
            if (args.size > MAX_ENTRIES) throw IllegalArgumentException("args exceeds $MAX_ENTRIES entries")
            if (args.any { it.length > MAX_PROMPT_CHARS }) throw IllegalArgumentException("an arg exceeds $MAX_PROMPT_CHARS characters")
            checkEnv(env, "env")
            if (agent != null && agent.isBlank()) throw IllegalArgumentException("agent must not be blank")
            if (model != null && !MODEL_PATTERN.matches(model)) {
                throw IllegalArgumentException("model must be 1 to 200 characters from letters, digits and . _ : / @ + -")
            }
            val chosenVia = via?.let { LaunchVia.of(it) ?: throw IllegalArgumentException("via must be 'ori' or 'direct'") }
            return OpenRequest(dir.normalize(), prompt?.takeIf { it.isNotBlank() }, args, env, agent, model, chosenVia)
        }
    }
}

fun closestBase(target: Path, bases: List<Path?>): Int? =
    bases.withIndex()
        .filter { (_, base) -> base != null && target.startsWith(base) }
        .maxByOrNull { (_, base) -> base!!.nameCount }
        ?.index
