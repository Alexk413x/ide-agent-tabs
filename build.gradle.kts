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
            untilBuild = provider { null }
        }
    }
    buildSearchableOptions = false
    instrumentCode = false
}

val pluginRepositoryDir = providers.gradleProperty("pluginRepositoryDir")
    .orElse(providers.systemProperty("user.home").map { "$it/.claude-studio-tabs/repository" })

val updatePluginsXml = tasks.register("updatePluginsXml") {
    val out = layout.buildDirectory.file("repository/updatePlugins.xml")
    val repoDir = pluginRepositoryDir
    val pluginVersion = version.toString()
    inputs.property("repoDir", repoDir)
    inputs.property("version", pluginVersion)
    outputs.file(out)
    doLast {
        val zipUrl = File(repoDir.get(), "claude-studio-tabs-$pluginVersion.zip").toPath().toUri()
        out.get().asFile.writeText(
            """
            <plugins>
              <plugin id="dev.alexk.claude-studio-tabs" url="$zipUrl" version="$pluginVersion">
                <idea-version since-build="262"/>
                <name>Claude Studio Tabs</name>
                <vendor>Alexk413x</vendor>
                <description>Opens a new Claude Code session in an editor tab.</description>
              </plugin>
            </plugins>
            """.trimIndent() + "\n"
        )
    }
}

tasks.register<Sync>("publishLocal") {
    description = "Copies the plugin zip and updatePlugins.xml into the local plugin repository."
    from(tasks.named("buildPlugin"))
    from(updatePluginsXml)
    into(pluginRepositoryDir)
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
        // A plain folder, not this Gradle repo: opening the repo starts an Android Gradle sync that stops
        // on a missing-SDK dialog, and a modal dialog blocks every tab the plugin opens.
        val sandboxProject = layout.buildDirectory.dir("sandbox-project").get().asFile
        doFirst { sandboxProject.mkdirs() }
        args(sandboxProject.absolutePath)
        jvmArgs(
            "-Dclaude.studio.tabs.endpoint.file=${layout.buildDirectory.file("sandbox-endpoint.json").get().asFile.absolutePath}",
            "-Ddisable.android.first.run=true",
            "-Didea.trust.all.projects=true",
            "-Djb.consents.confirmation.enabled=false",
            "-Djb.privacy.policy.text=<!--999.999-->",
        )
    }
}
