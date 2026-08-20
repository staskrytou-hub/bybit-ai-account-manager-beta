from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from paths import ENV_FILE


def load_local_env() -> None:
    load_dotenv(ENV_FILE, override=True)


def has_api_key() -> bool:
    load_local_env()
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def save_api_key(api_key: str) -> None:
    value = api_key.strip()
    if not value:
        raise ValueError("API key cannot be empty.")
    if "\n" in value or "\r" in value:
        raise ValueError("API key must be a single line.")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(ENV_FILE) + ".tmp")
    temp.write_text(f"OPENAI_API_KEY={value}\n", encoding="utf-8")
    temp.replace(ENV_FILE)
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    os.environ["OPENAI_API_KEY"] = value


def clear_api_key() -> None:
    if ENV_FILE.exists():
        ENV_FILE.unlink()
    os.environ.pop("OPENAI_API_KEY", None)
