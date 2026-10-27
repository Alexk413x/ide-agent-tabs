from __future__ import annotations

import os
import re
import shutil
from typing import Any, NamedTuple
from urllib.parse import quote

from .cli_run import run_cli
from .clock import iso, now_ms
from .editor_clis import find_editor_clis, resolve_editor_cli
from .files import ensure_private_dir, mtime_ms, read_text_if_exists, remove_file, write_atomically, write_new_private_file
from .jsjson import parse, stringify
from .parallel import run_all
from .register import RegisterContext, migrate_registrations, refresh_copy, tab_settings_file, tab_settings_text
from .server_copy import bundled_build, old_copy_exists, read_python, remove_old_copy
from .version import compare_versions

EXTENSION_ID = "alexk413x.ide-agent-tabs"
JETBRAINS_PLUGIN_ID = "dev.alexk.ide-agent-tabs"
JETBRAINS_SINCE_BUILD = "262.10315"
VSIX_NAME = "ide-agent-tabs.vsix"
JETBRAINS_ZIP_NAME = "ide-agent-tabs-jetbrains.zip"
MAX_ATTEMPTS = 3
LOCK_STALE_MS = 5 * 60_000
LIST_TIMEOUT_S = 15.0
INSTALL_TIMEOUT_S = 40.0
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,64}\Z")
_EXTENSION_LINE = re.compile(r"([\w-]+\.[\w.-]+)@(\S+)\Z")


class Bundle(NamedTuple):
    versions: dict[str, str]
    vsix: str
    zip: str


def parse_extension_list(stdout: str) -> dict[str, str]:
    extensions: dict[str, str] = {}
    for line in re.split(r"\r?\n", stdout):
        match = _EXTENSION_LINE.match(line.strip())
        if match:
            extensions[match.group(1).lower()] = match.group(2)
    return extensions


def file_url(file: str, platform: str) -> str:
    forward = file.replace("\\", "/") if platform == "win32" else file
    absolute = forward if forward.startswith("/") else f"/{forward}"
    return "file://" + quote(absolute, safe=";,/:@&=+$-_.!~*'()")


def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def update_plugins_xml(version: str, zip_url: str) -> str:
    return "\n".join(
        [
            "<plugins>",
            f'  <plugin id="{JETBRAINS_PLUGIN_ID}" url="{_escape_xml(zip_url)}" version="{_escape_xml(version)}">',
            f'    <idea-version since-build="{JETBRAINS_SINCE_BUILD}"/>',
            "    <name>Agent Tabs</name>",
            "    <vendor>Alexk413x</vendor>",
            "    <description>Opens AI coding-agent sessions in editor tabs.</description>",
            "  </plugin>",
            "</plugins>",
            "",
        ]
    )


def repository_version(xml: str) -> str | None:
    for tag in re.findall(r"<plugin\s[^>]*>", xml):
        if f'id="{JETBRAINS_PLUGIN_ID}"' not in tag:
            continue
        match = re.search(r'\sversion="([^"]*)"', tag)
        return match.group(1) if match else None
    return None


def installed_extension_version(ctx: RegisterContext, cli: str) -> str | None:
    stdout = run_cli(ctx.platform, ctx.env, cli, ["--list-extensions", "--show-versions"], LIST_TIMEOUT_S)
    return parse_extension_list(stdout).get(EXTENSION_ID)


def install_vsix(ctx: RegisterContext, cli: str, vsix: str) -> None:
    run_cli(ctx.platform, ctx.env, cli, ["--install-extension", vsix, "--force"], INSTALL_TIMEOUT_S)


def read_bundle(bundle_dir: str) -> Bundle:
    raw = parse(read_text_if_exists(os.path.join(bundle_dir, "versions.json")) or "")
    versions: dict[str, str] = {}
    for part in ("vscode", "jetbrains"):
        value = raw.get(part) if isinstance(raw, dict) else None
        if not isinstance(value, str) or not _VERSION.match(value):
            raise ValueError(f"versions.json has no valid {part} version")
        versions[part] = value
    return Bundle(versions, os.path.join(bundle_dir, VSIX_NAME), os.path.join(bundle_dir, JETBRAINS_ZIP_NAME))


def parse_sync_state(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        raw = parse(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("vscode"), str) or not isinstance(raw.get("jetbrains"), str):
        return None
    failures = raw.get("failures")
    state: dict[str, Any] = {
        "vscode": raw["vscode"],
        "jetbrains": raw["jetbrains"],
        "syncedAt": raw["syncedAt"] if isinstance(raw.get("syncedAt"), str) else "",
        "failures": failures if isinstance(failures, (int, float)) and not isinstance(failures, bool) else 0,
    }
    for key in ("server", "python"):
        if isinstance(raw.get(key), str):
            state[key] = raw[key]
    return state


def needs_sync(state: dict[str, Any] | None, bundled: dict[str, str]) -> bool:
    if state is None or state["vscode"] != bundled["vscode"] or state["jetbrains"] != bundled["jetbrains"]:
        return True
    return 0 < state["failures"] < MAX_ATTEMPTS


def next_sync_state(previous: dict[str, Any] | None, bundled: dict[str, str], failed: bool, now: int) -> dict[str, Any]:
    same = previous is not None and previous["vscode"] == bundled["vscode"] and previous["jetbrains"] == bundled["jetbrains"]
    failures = ((previous["failures"] if same and previous is not None else 0) + 1) if failed else 0
    state: dict[str, Any] = {**bundled, "syncedAt": iso(now), "failures": failures}
    for key in ("server", "python"):
        if previous is not None and previous.get(key):
            state[key] = previous[key]
    return state


def hook_message(bundled: dict[str, str], updated_editors: list[str], jetbrains_updated: bool) -> str | None:
    parts: list[str] = []
    if updated_editors:
        parts.append(f"updated the VS Code extension to {bundled['vscode']} in {', '.join(updated_editors)} (reload their windows)")
    if jetbrains_updated:
        parts.append(f"the JetBrains plugin {bundled['jetbrains']} is ready in each JetBrains IDE's plugin updates")
    return f"Agent Tabs: {'; '.join(parts)}." if parts else None


def _sync_file(home: str) -> str:
    return os.path.join(home, "synced.json")


def repository_dir(home: str) -> str:
    return os.path.join(home, "repository")


def read_sync_state(home: str) -> dict[str, Any] | None:
    try:
        return parse_sync_state(read_text_if_exists(_sync_file(home)))
    except OSError:
        return None


def write_sync_state(home: str, state: dict[str, Any]) -> None:
    write_atomically(_sync_file(home), stringify(state, 2) + "\n")


def append_log(home: str, message: str, now: int | None = None) -> None:
    ensure_private_dir(home)
    with open(os.path.join(home, "sync.log"), "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{iso(now_ms() if now is None else now)} {message}\n")


def try_lock(file: str, stale_ms: float = LOCK_STALE_MS, now: int | None = None) -> bool:
    at = now_ms() if now is None else now
    for _ in range(2):
        try:
            write_new_private_file(file, "")
            return True
        except FileExistsError:
            mtime = mtime_ms(file)
            if mtime is not None and at - mtime <= stale_ms:
                return False
            remove_file(file)
    return False


def publish_jetbrains(bundle: Bundle, repo_dir: str, platform: str) -> dict[str, Any]:
    xml_file = os.path.join(repo_dir, "updatePlugins.xml")
    existing = repository_version(read_text_if_exists(xml_file) or "")
    version = bundle.versions["jetbrains"]
    zip_name = f"ide-agent-tabs-{version}.zip"
    result = {"repository": repo_dir, "updatePluginsXml": xml_file, "url": file_url(xml_file, platform)}
    if existing is not None and compare_versions(existing, version) > 0:
        return {**result, "zip": os.path.join(repo_dir, f"ide-agent-tabs-{existing}.zip"), "version": existing, "changed": False}
    ensure_private_dir(repo_dir)
    zip_file = os.path.join(repo_dir, zip_name)
    shutil.copyfile(bundle.zip, zip_file)
    xml = update_plugins_xml(version, file_url(zip_file, platform))
    if read_text_if_exists(xml_file) != xml:
        write_atomically(xml_file, xml)
    for name in os.listdir(repo_dir):
        if re.match(r"ide-agent-tabs-.+\.zip\Z", name) and name != zip_name:
            remove_file(os.path.join(repo_dir, name))
    return {**result, "zip": zip_file, "version": version, "changed": existing != version}


def _sync_editors(ctx: RegisterContext, bundle: Bundle, errors: list[str]) -> tuple[list[str], bool]:
    def one(cli: str, file: str) -> str | None:
        try:
            installed = installed_extension_version(ctx, file)
            if installed is None or compare_versions(installed, bundle.versions["vscode"]) >= 0:
                return None
            install_vsix(ctx, file, bundle.vsix)
            return cli
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            errors.append(f"{cli}: {e}")
            return None

    editors = find_editor_clis(ctx.platform, ctx.env, ctx.user_home)
    updated = run_all([lambda c=c, f=f: one(c, f) for c, f in editors])
    jetbrains_updated = False
    if os.path.exists(repository_dir(ctx.home)):
        try:
            jetbrains_updated = publish_jetbrains(bundle, repository_dir(ctx.home), ctx.platform)["changed"]
        except (OSError, ValueError) as e:
            errors.append(f"jetbrains: {e}")
    return [c for c in updated if c is not None], jetbrains_updated


def _copy_needs(ctx: RegisterContext, previous: dict[str, Any] | None) -> tuple[str | None, bool]:
    build = bundled_build(ctx.source)
    server = None if previous is not None and previous.get("server") == build and not old_copy_exists(ctx.home) else build
    python_changed = (
        (previous or {}).get("python") != ctx.python
        or read_python(ctx.home) != ctx.python
        or read_text_if_exists(tab_settings_file(ctx)) != tab_settings_text(ctx)
    )
    return server, python_changed


def sync_hook(ctx: RegisterContext, bundle_dir: str, now: int | None = None) -> str | None:
    at = now_ms() if now is None else now
    bundle = read_bundle(bundle_dir)
    previous = read_sync_state(ctx.home)
    server, python_changed = _copy_needs(ctx, previous)
    ides = needs_sync(previous, bundle.versions)
    if not ides and server is None and not python_changed:
        return None
    ensure_private_dir(ctx.home)
    lock = os.path.join(ctx.home, "sync.lock")
    if not try_lock(lock, LOCK_STALE_MS, at):
        return None
    try:
        errors: list[str] = []
        synced: dict[str, Any] = {k: previous[k] for k in ("server", "python") if previous is not None and previous.get(k)}
        if server is not None or python_changed:
            try:
                refresh_copy(ctx)
                synced["server"] = bundled_build(ctx.source)
                synced["python"] = ctx.python
                _, failures = migrate_registrations(ctx)
                errors.extend(f"migration: {f}" for f in failures)
                if not failures and old_copy_exists(ctx.home):
                    remove_old_copy(ctx.home, ctx.source.version)
            except (OSError, ValueError, TimeoutError) as e:
                errors.append(f"server copy: {e}")
        ide_errors: list[str] = []
        updated, jetbrains_updated = _sync_editors(ctx, bundle, ide_errors) if ides else ([], False)
        if ides:
            state = next_sync_state(previous, bundle.versions, bool(ide_errors), at)
        else:
            state = {**(previous or {}), "syncedAt": iso(at)}
        write_sync_state(ctx.home, {**{k: v for k, v in state.items() if k not in ("server", "python")}, **synced})
        errors.extend(ide_errors)
        if errors:
            append_log(ctx.home, "\n".join(errors), at)
        return hook_message(bundle.versions, updated, jetbrains_updated)
    finally:
        remove_file(lock)


def sync_install(ctx: RegisterContext, bundle_dir: str, clis: list[str], jetbrains: bool, now: int | None = None) -> dict[str, Any]:
    at = now_ms() if now is None else now
    bundle = read_bundle(bundle_dir)
    errors: list[str] = []

    def one(name: str) -> dict[str, Any]:
        found = resolve_editor_cli(name, ctx.platform, ctx.env, ctx.user_home)
        if found is None:
            errors.append(f"{name}: not found")
            return {"cli": name, "ok": False, "error": "not found"}
        try:
            install_vsix(ctx, found.path, bundle.vsix)
            return {"cli": found.cli, "path": found.path, "ok": True}
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            errors.append(f"{name}: {e}")
            return {"cli": found.cli, "path": found.path, "ok": False, "error": str(e)}

    editors = run_all([lambda n=n: one(n) for n in clis])
    jetbrains_result = None
    if jetbrains:
        try:
            jetbrains_result = publish_jetbrains(bundle, repository_dir(ctx.home), ctx.platform)
        except (OSError, ValueError) as e:
            errors.append(f"jetbrains: {e}")
    write_sync_state(ctx.home, next_sync_state(read_sync_state(ctx.home), bundle.versions, bool(errors), at))
    if errors:
        append_log(ctx.home, "\n".join(errors), at)
    return {
        "bundled": bundle.versions,
        "vscode": {"version": bundle.versions["vscode"], "vsix": bundle.vsix, "editors": editors},
        "jetbrains": jetbrains_result,
        "errors": errors,
    }


def sync_status(ctx: RegisterContext, bundle_dir: str) -> dict[str, Any]:
    bundle = read_bundle(bundle_dir)

    def one(cli: str, file: str) -> dict[str, Any]:
        try:
            return {"cli": cli, "path": file, "installed": installed_extension_version(ctx, file)}
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            return {"cli": cli, "path": file, "installed": None, "error": str(e)}

    editors = run_all([lambda c=c, f=f: one(c, f) for c, f in find_editor_clis(ctx.platform, ctx.env, ctx.user_home)])
    repo_dir = repository_dir(ctx.home)
    xml_file = os.path.join(repo_dir, "updatePlugins.xml")
    try:
        xml = read_text_if_exists(xml_file)
    except OSError:
        xml = None
    return {
        "bundled": bundle.versions,
        "home": ctx.home,
        "synced": read_sync_state(ctx.home),
        "editors": editors,
        "jetbrains": {
            "repository": repo_dir,
            "exists": os.path.exists(repo_dir),
            "version": (repository_version(xml) if xml else None),
            "updatePluginsXml": xml_file,
            "url": file_url(xml_file, ctx.platform),
        },
    }
