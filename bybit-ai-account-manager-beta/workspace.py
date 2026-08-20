from __future__ import annotations

from pathlib import Path

from paths import WORKSPACE_DIR

MAX_TEXT_FILE_BYTES = 2_000_000


def _safe_path(filename: str) -> Path:
    name = filename.strip().replace("\\", "/")
    if not name or name.startswith("/"):
        raise ValueError("A relative workspace filename is required.")
    target = (WORKSPACE_DIR / name).resolve()
    root = WORKSPACE_DIR.resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path must stay inside Bybit AI Manager Workspace.")
    return target


def write_text_file(filename: str, content: str) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise ValueError("Workspace text writes are limited to 2 MB per file.")
    target = _safe_path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.relative_to(WORKSPACE_DIR)).replace("\\", "/")


def read_text_file(filename: str) -> str:
    target = _safe_path(filename)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(filename)
    if target.stat().st_size > MAX_TEXT_FILE_BYTES:
        raise ValueError("Workspace text reads are limited to 2 MB per file.")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("This is a binary file. Use the Artifacts panel to open it instead of read_workspace_file.") from exc


def list_workspace_files(limit: int = 500) -> list[str]:
    files = [p.relative_to(WORKSPACE_DIR).as_posix() for p in WORKSPACE_DIR.rglob("*") if p.is_file()]
    return sorted(files)[: max(1, min(limit, 2000))]
