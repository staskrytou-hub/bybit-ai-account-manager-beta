from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paths import SETTINGS_FILE

WORKSPACE_WRITE_POLICIES = {"ask", "always_allow"}
MODEL_PROFILES = {"economy", "balanced", "quality"}
IMAGE_QUALITIES = {"low", "medium", "high", "auto"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "workspace_write_policy": "always_allow",
    "model_profile": "balanced",
    "image_quality": "medium",
    "use_sdk_session_history": False,
    "preview_default_port": 8000,
    "auto_open_preview_browser": True,
    "autonomy_max_steps": 8,
    "autonomy_token_budget": 60000,
    "autonomy_daily_token_budget": 300000,
    "autonomy_retry_limit": 2,
    "autonomy_step_max_turns": 10,
    "autonomy_planner_max_turns": 5,
    "context_history_messages": 10,
    "context_history_chars": 18000,
    "api_rate_limit_retries": 6,
    "api_rate_limit_max_wait_seconds": 45,
    "auto_learn_corrections": True,
}


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if value.strip().lower() in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_choice(value: Any, allowed: set[str], default: str) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in allowed else default


def load_settings() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    return {
        "workspace_write_policy": _coerce_choice(data.get("workspace_write_policy"), WORKSPACE_WRITE_POLICIES, str(DEFAULT_SETTINGS["workspace_write_policy"])),
        "model_profile": _coerce_choice(data.get("model_profile"), MODEL_PROFILES, str(DEFAULT_SETTINGS["model_profile"])),
        "image_quality": _coerce_choice(data.get("image_quality"), IMAGE_QUALITIES, str(DEFAULT_SETTINGS["image_quality"])),
        "use_sdk_session_history": _coerce_bool(data.get("use_sdk_session_history"), bool(DEFAULT_SETTINGS["use_sdk_session_history"])),
        "preview_default_port": _coerce_int(data.get("preview_default_port"), int(DEFAULT_SETTINGS["preview_default_port"]), 1024, 65535),
        "auto_open_preview_browser": _coerce_bool(data.get("auto_open_preview_browser"), bool(DEFAULT_SETTINGS["auto_open_preview_browser"])),
        "autonomy_max_steps": _coerce_int(data.get("autonomy_max_steps"), int(DEFAULT_SETTINGS["autonomy_max_steps"]), 1, 24),
        "autonomy_token_budget": _coerce_int(data.get("autonomy_token_budget"), int(DEFAULT_SETTINGS["autonomy_token_budget"]), 5000, 1000000),
        "autonomy_daily_token_budget": _coerce_int(data.get("autonomy_daily_token_budget"), int(DEFAULT_SETTINGS["autonomy_daily_token_budget"]), 10000, 5000000),
        "autonomy_retry_limit": _coerce_int(data.get("autonomy_retry_limit"), int(DEFAULT_SETTINGS["autonomy_retry_limit"]), 0, 5),
        "autonomy_step_max_turns": _coerce_int(data.get("autonomy_step_max_turns"), int(DEFAULT_SETTINGS["autonomy_step_max_turns"]), 2, 30),
        "autonomy_planner_max_turns": _coerce_int(data.get("autonomy_planner_max_turns"), int(DEFAULT_SETTINGS["autonomy_planner_max_turns"]), 2, 12),
        "context_history_messages": _coerce_int(data.get("context_history_messages"), int(DEFAULT_SETTINGS["context_history_messages"]), 4, 40),
        "context_history_chars": _coerce_int(data.get("context_history_chars"), int(DEFAULT_SETTINGS["context_history_chars"]), 4000, 60000),
        "api_rate_limit_retries": _coerce_int(data.get("api_rate_limit_retries"), int(DEFAULT_SETTINGS["api_rate_limit_retries"]), 0, 12),
        "api_rate_limit_max_wait_seconds": _coerce_int(data.get("api_rate_limit_max_wait_seconds"), int(DEFAULT_SETTINGS["api_rate_limit_max_wait_seconds"]), 5, 180),
        "auto_learn_corrections": _coerce_bool(data.get("auto_learn_corrections"), bool(DEFAULT_SETTINGS["auto_learn_corrections"])),
    }


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    current.update(values)
    clean: dict[str, Any] = {
        "workspace_write_policy": _coerce_choice(current.get("workspace_write_policy"), WORKSPACE_WRITE_POLICIES, str(DEFAULT_SETTINGS["workspace_write_policy"])),
        "model_profile": _coerce_choice(current.get("model_profile"), MODEL_PROFILES, str(DEFAULT_SETTINGS["model_profile"])),
        "image_quality": _coerce_choice(current.get("image_quality"), IMAGE_QUALITIES, str(DEFAULT_SETTINGS["image_quality"])),
        "use_sdk_session_history": _coerce_bool(current.get("use_sdk_session_history"), bool(DEFAULT_SETTINGS["use_sdk_session_history"])),
        "preview_default_port": _coerce_int(current.get("preview_default_port"), int(DEFAULT_SETTINGS["preview_default_port"]), 1024, 65535),
        "auto_open_preview_browser": _coerce_bool(current.get("auto_open_preview_browser"), bool(DEFAULT_SETTINGS["auto_open_preview_browser"])),
        "autonomy_max_steps": _coerce_int(current.get("autonomy_max_steps"), int(DEFAULT_SETTINGS["autonomy_max_steps"]), 1, 24),
        "autonomy_token_budget": _coerce_int(current.get("autonomy_token_budget"), int(DEFAULT_SETTINGS["autonomy_token_budget"]), 5000, 1000000),
        "autonomy_daily_token_budget": _coerce_int(current.get("autonomy_daily_token_budget"), int(DEFAULT_SETTINGS["autonomy_daily_token_budget"]), 10000, 5000000),
        "autonomy_retry_limit": _coerce_int(current.get("autonomy_retry_limit"), int(DEFAULT_SETTINGS["autonomy_retry_limit"]), 0, 5),
        "autonomy_step_max_turns": _coerce_int(current.get("autonomy_step_max_turns"), int(DEFAULT_SETTINGS["autonomy_step_max_turns"]), 2, 30),
        "autonomy_planner_max_turns": _coerce_int(current.get("autonomy_planner_max_turns"), int(DEFAULT_SETTINGS["autonomy_planner_max_turns"]), 2, 12),
        "context_history_messages": _coerce_int(current.get("context_history_messages"), int(DEFAULT_SETTINGS["context_history_messages"]), 4, 40),
        "context_history_chars": _coerce_int(current.get("context_history_chars"), int(DEFAULT_SETTINGS["context_history_chars"]), 4000, 60000),
        "api_rate_limit_retries": _coerce_int(current.get("api_rate_limit_retries"), int(DEFAULT_SETTINGS["api_rate_limit_retries"]), 0, 12),
        "api_rate_limit_max_wait_seconds": _coerce_int(current.get("api_rate_limit_max_wait_seconds"), int(DEFAULT_SETTINGS["api_rate_limit_max_wait_seconds"]), 5, 180),
        "auto_learn_corrections": _coerce_bool(current.get("auto_learn_corrections"), bool(DEFAULT_SETTINGS["auto_learn_corrections"])),
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(SETTINGS_FILE) + ".tmp")
    temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(SETTINGS_FILE)
    return clean
