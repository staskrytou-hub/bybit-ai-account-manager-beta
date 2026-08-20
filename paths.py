from __future__ import annotations

import os
from pathlib import Path


def app_home() -> Path:
    override = os.getenv("STAN_AI_HOME")
    if override:
        return Path(override).expanduser().resolve()
    # Desktop builds keep runtime state outside the repository.
    if os.name == "nt":
        local = os.getenv("LOCALAPPDATA", "").strip()
        if local:
            return (Path(local) / "BybitAIAccountManager").resolve()
    return Path(__file__).resolve().parent


APP_HOME = app_home()
DATA_DIR = APP_HOME / "data"
LOG_DIR = APP_HOME / "logs"
ASSETS_DIR = APP_HOME / "assets"
WORKSPACE_DIR = APP_HOME / "workspace"
ENV_FILE = APP_HOME / ".env.local"
MEMORY_DB = DATA_DIR / "memory.db"
CONVERSATION_DB = DATA_DIR / "conversation.db"
USAGE_DB = DATA_DIR / "usage.db"
TASKS_DB = DATA_DIR / "tasks.db"
AUTONOMY_DB = DATA_DIR / "autonomy.db"
CHAT_STORE_DB = DATA_DIR / "chat_store.db"
ARTIFACTS_DB = DATA_DIR / "artifacts.db"
TRADING_DB = DATA_DIR / "trading.db"
TRADING_RESEARCH_DB = DATA_DIR / "trading_research.db"
SETTINGS_FILE = DATA_DIR / "settings.json"
ACTION_LOG = LOG_DIR / "actions.jsonl"

for folder in (DATA_DIR, LOG_DIR, WORKSPACE_DIR):
    folder.mkdir(parents=True, exist_ok=True)
