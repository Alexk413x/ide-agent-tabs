package dev.alexk.ideagenttabs

// Pure module: no IDE or Netty types, so profile and request validation test without a running IDE.

const val MAX_PROMPT_CHARS = 30_000
const val MAX_ENTRIES = 64
const val MAX_INPUT_CHARS = 500

val MODEL_PATTERN = Regex("[A-Za-z0-9._:/@+-]{1,200}")

const val PLUGIN_ENV_PREFIX = "IDE_AGENT_TABS_"
const val STARTUP_ENV = "JEDITERM_SOURCE"

fun isReservedEnv(name: String): Boolean =
    name.startsWith(PLUGIN_ENV_PREFIX, ignoreCase = true) || name.startsWith(STARTUP_ENV, ignoreCase = true)
