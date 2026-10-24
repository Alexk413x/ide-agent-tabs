package dev.alexk.ideagenttabs

import com.google.gson.JsonObject
import java.nio.file.Files
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.attribute.FileTime
import java.time.Instant

// Pure module: no IDE or Netty types, so the registry file tests without a running IDE.

const val PROTOCOL_VERSION = 1
const val ENDPOINT_BEAT_MS = 60_000L

fun endpointJson(
    product: String,
    version: String,
    pid: Long,
    url: String,
    token: String,
    startedAt: Long,
) = JsonObject().apply {
    addProperty("protocol", PROTOCOL_VERSION)
    addProperty("ide", "jetbrains")
    addProperty("product", product)
    addProperty("version", version)
    addProperty("pid", pid)
    addProperty("url", url)
    addProperty("token", token)
    addProperty("startedAt", startedAt)
    addProperty("beatMs", ENDPOINT_BEAT_MS)
}

fun beatEndpoint(target: Path, content: String, now: Instant = Instant.now()) {
    try {
        Files.setLastModifiedTime(target, FileTime.from(now))
    } catch (e: NoSuchFileException) {
        writeAtomically(target, content, private = true)
    }
}
