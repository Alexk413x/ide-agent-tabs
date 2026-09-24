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

val claudeCodePlugin = providers.gradleProperty("claudeCodePluginPath")

intellijPlatformTesting.runIde.register("runIdeWithClaude") {
    plugins {
        localPlugin(claudeCodePlugin)
    }
    task {
        args(layout.projectDirectory.asFile.absolutePath)
        jvmArgs(
            "-Ddisable.android.first.run=true",
            "-Didea.trust.all.projects=true",
            "-Djb.consents.confirmation.enabled=false",
            "-Djb.privacy.policy.text=<!--999.999-->",
        )
    }
}
