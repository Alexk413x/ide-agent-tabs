from __future__ import annotations

import os
from collections.abc import Mapping

HOME_ENV = "IDE_AGENT_TABS_HOME"


def agent_tabs_home(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get(HOME_ENV) or os.path.join(os.path.expanduser("~"), ".ide-agent-tabs")
