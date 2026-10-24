package dev.alexk.ideagenttabs

import com.google.gson.JsonObject

// Pure module: no IDE or Netty types, so profiles and settings test without a running IDE.

const val DEFAULT_AGENT = "claude"
const val AGENTS_FILE = "agents.json"
const val CONFIG_FILE = "config.json"

private val PROFILE_NAME = Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

data class AgentProfile(
    val name: String,
    val label: String,
    val command: String,
    val args: List<String> = emptyList(),
    val promptFlag: String? = null,
    val env: Map<String, String> = emptyMap(),
    val icon: String? = null,
    val modelFlag: String? = null,
) {
    fun launch(
        prompt: String?,
        callerArgs: List<String> = emptyList(),
        callerEnv: Map<String, String> = emptyMap(),
        model: String? = null,
    ) = AgentLaunch(
        agent = name,
        command = command,
        args = args + listOfNotNull(modelFlag?.let { flag -> model?.let { listOf(flag, it) } }).flatten() + callerArgs +
            listOfNotNull(promptFlag?.takeIf { prompt != null }),
        prompt = prompt,
        env = env + callerEnv,
        via = LaunchVia.DIRECT,
    )
}

class AgentLaunch(
    val agent: String,
    val command: String,
    val args: List<String>,
    val prompt: String?,
    val env: Map<String, String>,
    val via: LaunchVia = LaunchVia.DIRECT,
)

// Same rows as list_agents in claude-plugin/mcp/src/ide_agent_tabs/service.py, less the agents that aren't installed.
data class AgentEntry(val name: String, val label: String, val profile: AgentProfile, val via: LaunchVia? = null)

class LocalVariant(val name: String, val base: String, val shown: (SharedSettings) -> Boolean)

val LOCAL_VARIANTS = listOf(
    LocalVariant("codex-lmstudio", "codex") { it.showLmStudioAgents },
    LocalVariant("codex-ollama", "codex") { it.showOllamaAgents },
)

val AGENT_ALIASES = mapOf("codex-local" to "codex-ollama")

fun findProfile(profiles: List<AgentProfile>, name: String?): AgentProfile? {
    if (name == null) return null
    return profiles.firstOrNull { it.name == name } ?: AGENT_ALIASES[name]?.let { alias -> profiles.firstOrNull { it.name == alias } }
}

fun agentEntries(profiles: List<AgentProfile>, ori: DetectedOri?, toggles: SharedSettings, installed: (AgentProfile) -> Boolean): List<AgentEntry> =
    profiles.filter { p -> LOCAL_VARIANTS.none { it.name == p.name } }.flatMap { p ->
        listOfNotNull(
            AgentEntry(p.name, p.label, p).takeIf { installed(p) },
            AgentEntry("$ORI_PREFIX${p.name}", "${p.label} - Ori", p, LaunchVia.ORI).takeIf { toggles.showOriAgents && oriLaunchable(p.name, ori) },
        ) + LOCAL_VARIANTS.filter { it.base == p.name && it.shown(toggles) }.mapNotNull { v ->
            findProfile(profiles, v.name)?.takeIf(installed)?.let { AgentEntry(it.name, it.label, it) }
        }
    }

// Same strings as CODEX_TAB_ARGS in claude-plugin/mcp/src/ide_agent_tabs/profiles.py, which explains them; mcp/tests/test_codex_tab.py checks both.
val CODEX_TAB_ARGS = listOf(
    "--no-daemon",
    "-c",
    "mcp_servers.ide-agent-tabs={ command = 'python3', args = ['-I', '-S', '-c', '''import os,runpy;h=os.environ.get('IDE_AGENT_TABS_HOME') or os.path.join(os.path.expanduser('~'),'.ide-agent-tabs');runpy.run_path(os.path.join(h,'mcp','py','launch','mcp_server.py'),run_name='__main__')'''], env_vars = ['IDE_AGENT_TABS_ID', 'IDE_AGENT_TABS_AGENT', 'IDE_AGENT_TABS_HOME'], tool_timeout_sec = 660 }",
    "-c",
    "hooks.UserPromptSubmit=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'UserPromptSubmit', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PostToolUse=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PostToolUse', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.PermissionRequest=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'PermissionRequest', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Stop=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Stop', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 10 }] }]",
    "-c",
    "hooks.Interrupt=[{ hooks = [{ type = 'mcp_tool', server = 'ide-agent-tabs', tool = 'agent_tabs_hook', input = { event = 'Interrupt', session_id = '\${session_id}', turn_id = '\${turn_id}' }, timeout = 3 }] }]",
    "-c",
    "hooks.state={ '/<session-flags>/config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, 'C:\\<session-flags>\\config.toml:user_prompt_submit:0:0' = { trusted_hash = 'sha256:aac36b4c0cfafe0f4ae641176bcc1ab25ae590dbe3be9268f7570b55ab4afe89' }, '/<session-flags>/config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, 'C:\\<session-flags>\\config.toml:post_tool_use:0:0' = { trusted_hash = 'sha256:75aa06c6f44c8918fe729537b56d5f498c931e032f5d89499593b4cd67ba335e' }, '/<session-flags>/config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, 'C:\\<session-flags>\\config.toml:permission_request:0:0' = { trusted_hash = 'sha256:5e1483151807db1577272adc730b9ffe56c96d22b1a8fe7c7ff6d6efe42f3626' }, '/<session-flags>/config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, 'C:\\<session-flags>\\config.toml:stop:0:0' = { trusted_hash = 'sha256:a97c883d6b41f88f6879ce99d0d343a7069f3fded56573aaa2b15fc5bbd01c6f' }, '/<session-flags>/config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' }, 'C:\\<session-flags>\\config.toml:interrupt:0:0' = { trusted_hash = 'sha256:c2704217d5db401ed47f178ff9db1a7be09662f73b3e55753f8600e42bd53165' } }",
)

val BUILTIN_PROFILES = listOf(
    AgentProfile("claude", "Claude Code", "claude", modelFlag = "--model"),
    AgentProfile("codex", "Codex", "codex", CODEX_TAB_ARGS, modelFlag = "-m"),
    AgentProfile("agy", "Antigravity CLI", "agy", promptFlag = "-i", modelFlag = "--model"),
    AgentProfile("copilot", "Copilot CLI", "copilot", promptFlag = "-i", modelFlag = "--model"),
    AgentProfile("grok", "Grok Build", "grok", modelFlag = "-m"),
    AgentProfile("pi", "Pi", "pi", modelFlag = "--model"),
    AgentProfile("hermes", "Hermes", "hermes", listOf("chat"), promptFlag = "-q", modelFlag = "-m"),
    AgentProfile("opencode", "OpenCode", "opencode", promptFlag = "--prompt", modelFlag = "-m"),
    AgentProfile("qwen", "Qwen Code", "qwen", promptFlag = "-i", modelFlag = "-m"),
    AgentProfile("goose", "Goose", "goose", listOf("run", "-s"), promptFlag = "-t", modelFlag = "--model"),
    // Without --local-provider, --oss stops at a picker between LM Studio and Ollama.
    AgentProfile("codex-lmstudio", "Codex - LM Studio", "codex", CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "lmstudio"), modelFlag = "-m"),
    AgentProfile("codex-ollama", "Codex - Ollama", "codex", CODEX_TAB_ARGS + listOf("--oss", "--local-provider", "ollama"), modelFlag = "-m"),
)

fun parseProfiles(text: String): List<AgentProfile> {
    val root = parseJsonObject(text, AGENTS_FILE)
    return root.entrySet().map { (name, value) ->
        if (!PROFILE_NAME.matches(name)) throw IllegalArgumentException("profile name '$name' must be letters, digits, '.', '_' or '-'")
        if (!value.isJsonObject) throw IllegalArgumentException("profile $name must be an object")
        val obj = value.asJsonObject
        val command = obj.string("command", "$name.command")
        if (command.isNullOrBlank() || '\u0000' in command) throw IllegalArgumentException("profile $name needs a command")
        val args = obj.stringList("args", "$name.args")
        if (args.size > MAX_ENTRIES) throw IllegalArgumentException("$name.args exceeds $MAX_ENTRIES entries")
        if (args.any { '\u0000' in it }) throw IllegalArgumentException("$name.args holds a NUL")
        val promptFlag = obj.flag(name, "promptFlag")
        val modelFlag = obj.flag(name, "modelFlag")
        val env = obj.stringMap("env", "$name.env")
        checkEnv(env, "$name.env")
        AgentProfile(
            name = name,
            label = obj.string("label", "$name.label")?.takeIf { it.isNotBlank() } ?: name,
            command = command,
            args = args,
            promptFlag = promptFlag,
            env = env,
            icon = obj.string("icon", "$name.icon")?.takeIf { it.isNotBlank() },
            modelFlag = modelFlag,
        )
    }
}

private fun JsonObject.flag(profile: String, key: String): String? {
    val value = string(key, "$profile.$key")
    if (value != null && (value.isBlank() || '\u0000' in value)) throw IllegalArgumentException("$profile.$key must not be blank")
    return value
}

fun mergeProfiles(builtins: List<AgentProfile>, custom: List<AgentProfile>): List<AgentProfile> {
    val byName = custom.associateBy { it.name }
    return builtins.map { byName[it.name] ?: it } + custom.filter { c -> builtins.none { it.name == c.name } }
}

fun checkEnv(env: Map<String, String>, field: String) {
    if (env.size > MAX_ENTRIES) throw IllegalArgumentException("$field exceeds $MAX_ENTRIES entries")
    for ((name, value) in env) {
        if (name.isBlank() || name.any { it == '=' || it.isWhitespace() || it == '\u0000' }) {
            throw IllegalArgumentException("$field name is not a valid variable name: '$name'")
        }
        if (isReservedEnv(name)) throw IllegalArgumentException("$field name $name is reserved by the plugin")
        if (value.length > MAX_PROMPT_CHARS || '\u0000' in value) {
            throw IllegalArgumentException("$field $name is longer than $MAX_PROMPT_CHARS characters or holds a NUL")
        }
    }
}
