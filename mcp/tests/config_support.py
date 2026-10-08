from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from typing import Any

from ide_agent_tabs.register import RegisterContext
from ide_agent_tabs.server_copy import Source

SERVER_SCRIPT = "import sys\nsys.stdout.write(repr([__file__, sys.argv[1:]]))\n"


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def make_source(root: str, version: str = "0.9.0", server: str = SERVER_SCRIPT) -> Source:
    mcp = os.path.join(root, "plugin", "mcp")
    write(os.path.join(mcp, "src", "ide_agent_tabs", "__init__.py"), "")
    write(os.path.join(mcp, "src", "ide_agent_tabs", "catalog.json"), "{}\n")
    write(os.path.join(mcp, "launch", "mcp_server.py"), server)
    write(os.path.join(mcp, "launch", "agent_hook.py"), "import sys\nsys.stdout.write('hook ' + ' '.join(sys.argv[1:]))\n")
    write(os.path.join(mcp, "launch", "agent-tabs"), "#!/bin/sh\n")
    scripts = os.path.join(root, "plugin", "dist", "launch")
    for name in ("agent-launch.ps1", "agent-launch.sh", "agent-launch.fish"):
        write(os.path.join(scripts, name), f"# {name}\n")
    return Source(mcp, scripts, version)


FAKE_AGENT = r"""
import json, os, sys
NAME = "ide-agent-tabs"
agent, args = sys.argv[1], sys.argv[2:]
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.txt"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"agent": agent, "args": args, "cwd": os.getcwd()}) + "\n")
if agent == "codex":
    store = os.path.join(os.environ["CODEX_HOME"], "fake-mcp.json")
    servers = json.load(open(store)) if os.path.exists(store) else {}
    sys.stderr.write("WARNING: proceeding, even though we could not create PATH aliases\n")
    def write_toml():
        file = os.path.join(os.environ["CODEX_HOME"], "config.toml")
        head = open(file).read().split("[mcp_servers.")[0] if os.path.exists(file) else ""
        tables = ["[mcp_servers." + n + "]\ncommand = " + json.dumps(s["command"]) + "\nargs = " + json.dumps(s["args"]) + "\n" for n, s in servers.items()]
        open(file, "w").write(head + "\n".join(tables))
    if args[1] == "get":
        if NAME not in servers:
            sys.stderr.write("Error: No MCP server named 'ide-agent-tabs' found.\n")
            sys.exit(1)
        print(json.dumps({"name": NAME, "enabled": True, "transport": {"type": "stdio", **servers[NAME]}}, indent=2))
    elif args[1] == "add":
        rest = args[args.index("--") + 1:]
        servers[args[2]] = {"command": rest[0], "args": rest[1:]}
        json.dump(servers, open(store, "w"))
        write_toml()
    elif args[1] == "remove":
        servers.pop(args[2], None)
        json.dump(servers, open(store, "w"))
        write_toml()
elif agent == "editor":
    state = os.path.join(here, args.pop(0) + ".json")
    if args[:1] == ["--list-extensions"]:
        for ext in json.load(open(state)) if os.path.exists(state) else []:
            print(ext)
    elif args[:1] == ["--install-extension"]:
        json.dump(["alexk413x.ide-agent-tabs@" + os.environ.get("FAKE_VERSION", "9.9.9")], open(state, "w"))
"""


def fake_cli(bin_dir: str, name: str, kind: str | None = None) -> str:
    script = os.path.join(bin_dir, "fake_agent.py")
    if not os.path.exists(script):
        write(script, FAKE_AGENT)
    if kind is None:
        path = os.path.join(bin_dir, f"{name}.cmd" if sys.platform == "win32" else name)
        write(path, "")
        return path
    words = f"{kind} {name}" if kind == "editor" else kind
    if sys.platform == "win32":
        return write(
            os.path.join(bin_dir, f"{name}.cmd"), f'@"{sys.executable}" -I "%~dp0fake_agent.py" {words} %*\r\n@exit /b %ERRORLEVEL%\r\n'
        )
    path = write(os.path.join(bin_dir, name), f'#!/bin/sh\nexec "{sys.executable}" -I "${{0%/*}}/fake_agent.py" {words} "$@"\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def calls(bin_dir: str) -> list[dict[str, Any]]:
    path = os.path.join(bin_dir, "calls.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def system_env(extra: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "COMSPEC", "WINDIR", "TEMP", "TMP")}
    env.update(extra)
    return env


def context(source: Source, user_home: str, env: Mapping[str, str], python: str = sys.executable) -> RegisterContext:
    return RegisterContext(
        source,
        os.path.join(user_home, ".ide-agent-tabs"),
        "linux" if sys.platform.startswith("linux") else sys.platform,
        env,
        user_home,
        python,
    )


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()
