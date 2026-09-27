import org.jetbrains.intellij.platform.gradle.IntelliJPlatformType

plugins {
    id("org.jetbrains.kotlin.jvm") version "2.4.20"
    id("org.jetbrains.intellij.platform") version "2.19.0"
}

group = "dev.alexk.ideagenttabs"
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

val studioPath = providers.gradleProperty("studioPath").filter { it.isNotBlank() }
val platformVersion = providers.gradleProperty("platformVersion").get()

dependencies {
    intellijPlatform {
        if (studioPath.isPresent) {
            local(studioPath)
        } else {
            intellijIdea(platformVersion)
        }
        bundledPlugin("org.jetbrains.plugins.terminal")
        pluginVerifier()
    }
    testImplementation("junit:junit:4.13.2")
}

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = "262.10315"
            untilBuild = provider { null }
        }
    }
    pluginVerification {
        ides {
            create(IntelliJPlatformType.IntellijIdea, platformVersion)
            create(IntelliJPlatformType.AndroidStudio, providers.gradleProperty("verifierAndroidStudioVersion"))
        }
    }
    buildSearchableOptions = false
    instrumentCode = false
}

val pluginRepositoryDir = providers.gradleProperty("pluginRepositoryDir")
    .orElse(providers.systemProperty("user.home").map { "$it/.ide-agent-tabs/repository" })

val updatePluginsXml = tasks.register("updatePluginsXml") {
    val out = layout.buildDirectory.file("repository/updatePlugins.xml")
    val repoDir = pluginRepositoryDir
    val pluginVersion = version.toString()
    inputs.property("repoDir", repoDir)
    inputs.property("version", pluginVersion)
    outputs.file(out)
    doLast {
        val zipUrl = File(repoDir.get(), "ide-agent-tabs-$pluginVersion.zip").toPath().toUri()
        out.get().asFile.writeText(
            """
            <plugins>
              <plugin id="dev.alexk.ide-agent-tabs" url="$zipUrl" version="$pluginVersion">
                <idea-version since-build="262.10315"/>
                <name>Agent Tabs</name>
                <vendor>Alexk413x</vendor>
                <description>Opens AI coding-agent sessions in editor tabs.</description>
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

val claudeCodePluginPath = providers.gradleProperty("claudeCodePluginPath").filter { it.isNotBlank() }

if (claudeCodePluginPath.isPresent) {
    intellijPlatformTesting.runIde.register("runIdeWithClaude") {
        prepareSandboxTask {
            from(claudeCodePluginPath) {
                into("claude-code-jetbrains-plugin")
            }
        }
        task {
            // Launched from a Claude Code session, the sandbox would inherit NO_COLOR (which turns claude's
            // color off) and CLAUDE_CODE_SSE_PORT (which ties sandbox sessions to the outer IDE's plugin).
            val inherited = Regex("^(NO_COLOR|CLAUDECODE|CLAUDE_.*|ENABLE_IDE_INTEGRATION)$")
            doFirst {
                (this as JavaExec).environment.keys.removeIf { inherited.matches(it) }
            }
            // A plain folder, not this Gradle repo: opening the repo starts an Android Gradle sync that stops
            // on a missing-SDK dialog, and a modal dialog blocks every tab the plugin opens.
            val sandboxProject = layout.buildDirectory.dir("sandbox-project").get().asFile
            doFirst { sandboxProject.mkdirs() }
            args(sandboxProject.absolutePath)
            jvmArgs(
                "-Dide.agent.tabs.home=${layout.buildDirectory.dir("sandbox-home").get().asFile.absolutePath}",
                "-Ddisable.android.first.run=true",
                "-Didea.trust.all.projects=true",
                "-Djb.consents.confirmation.enabled=false",
                "-Djb.privacy.policy.text=<!--999.999-->",
            )
        }
    }
} else {
    tasks.register("runIdeWithClaude") {
        doFirst {
            throw GradleException("Set claudeCodePluginPath in ~/.gradle/gradle.properties to the Claude Code plugin folder.")
        }
    }
}
