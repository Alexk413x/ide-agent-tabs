package dev.alexk.ideagenttabs

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonObject
import com.google.gson.JsonParseException
import com.google.gson.JsonParser

// Pure module: no IDE or Netty types, so the JSON rules test without a running IDE.

internal val PRETTY_JSON: Gson = GsonBuilder().setPrettyPrinting().disableHtmlEscaping().create()

internal fun JsonObject.toPrettyText(): String = PRETTY_JSON.toJson(this) + "\n"

private fun parseObject(text: String, notJson: (JsonParseException) -> String, notObject: String): JsonObject {
    val json = try {
        JsonParser.parseString(text)
    } catch (e: JsonParseException) {
        throw IllegalArgumentException(notJson(e))
    }
    if (!json.isJsonObject) throw IllegalArgumentException(notObject)
    return json.asJsonObject
}

internal fun parseJsonObject(text: String, file: String): JsonObject =
    parseObject(text, { "$file is not JSON: ${it.message}" }, "$file must hold a JSON object")

internal fun parseBody(body: String): JsonObject =
    parseObject(body, { "body is not JSON" }, "body must be a JSON object")

internal fun JsonObject.string(key: String, label: String = key): String? {
    val value = get(key)
    if (value == null || value.isJsonNull) return null
    if (!value.isJsonPrimitive || !value.asJsonPrimitive.isString) throw IllegalArgumentException("$label must be a string")
    return value.asString
}

internal fun JsonObject.required(key: String): String =
    string(key)?.takeIf { it.isNotBlank() } ?: throw IllegalArgumentException("$key is required")

internal fun JsonObject.boolean(key: String, label: String = key): Boolean? {
    val value = get(key)
    if (value == null || value.isJsonNull) return null
    if (!value.isJsonPrimitive || !value.asJsonPrimitive.isBoolean) throw IllegalArgumentException("$label must be true or false")
    return value.asBoolean
}

internal fun JsonObject.stringList(key: String, label: String = key): List<String> {
    val value = get(key)
    if (value == null || value.isJsonNull) return emptyList()
    if (!value.isJsonArray) throw IllegalArgumentException("$label must be an array of strings")
    return value.asJsonArray.map {
        if (!it.isJsonPrimitive || !it.asJsonPrimitive.isString) throw IllegalArgumentException("$label must be an array of strings")
        it.asString
    }
}

internal fun JsonObject.stringMap(key: String, label: String = key): Map<String, String> {
    val value = get(key)
    if (value == null || value.isJsonNull) return emptyMap()
    if (!value.isJsonObject) throw IllegalArgumentException("$label must be an object of strings")
    return value.asJsonObject.entrySet().associate { (k, v) ->
        if (!v.isJsonPrimitive || !v.asJsonPrimitive.isString) throw IllegalArgumentException("$label.$k must be a string")
        k to v.asString
    }
}

internal fun String.hasControlChars(): Boolean = any { Character.getType(it) == Character.CONTROL.toInt() }
