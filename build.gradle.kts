plugins {
    id("org.jetbrains.kotlin.jvm") version "2.4.20"
    id("org.jetbrains.intellij.platform") version "2.19.0"
}

group = "dev.alexk.claudestudiotabs"
version = providers.gradleProperty("pluginVersion").get()

kotlin {
    jvmToolchain(25)
}

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        local(providers.gradleProperty("studioPath"))
        bundledPlugin("org.jetbrains.plugins.terminal")
    }
    testImplementation("junit:junit:4.13.2")
}

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = "262"
            untilBuild = "262.*"
        }
    }
    buildSearchableOptions = false
    instrumentCode = false
}

val claudeCodePluginZip = tasks.register<Zip>("claudeCodePluginZip") {
    from(providers.gradleProperty("claudeCodePluginPath")) {
        into("claude-code-jetbrains-plugin")
    }
    archiveFileName = "claude-code-jetbrains-plugin.zip"
    destinationDirectory = layout.buildDirectory.dir("sandbox-plugins")
}

intellijPlatformTesting.runIde.register("runIdeWithClaude") {
    plugins {
        localPlugin(layout.buildDirectory.file("sandbox-plugins/claude-code-jetbrains-plugin.zip").get().asFile)
    }
    task {
        // Launched from a Claude Code session, the sandbox would inherit NO_COLOR (which turns claude's
        // color off) and CLAUDE_CODE_SSE_PORT (which ties sandbox sessions to the outer IDE's plugin).
        val inherited = Regex("^(NO_COLOR|CLAUDECODE|CLAUDE_.*|ENABLE_IDE_INTEGRATION)$")
        doFirst {
            (this as JavaExec).environment.keys.removeIf { inherited.matches(it) }
        }
        dependsOn(claudeCodePluginZip)
        args(layout.projectDirectory.asFile.absolutePath)
        jvmArgs(
            "-Ddisable.android.first.run=true",
            "-Didea.trust.all.projects=true",
            "-Djb.consents.confirmation.enabled=false",
            "-Djb.privacy.policy.text=<!--999.999-->",
        )
    }
}
