from __future__ import annotations

from pathlib import Path
from typing import Any

from artifacts import list_artifacts
from paths import WORKSPACE_DIR


def verify_artifacts(after_id: int) -> dict[str, Any]:
    artifacts = list_artifacts(after_id=after_id, limit=500)
    existing = [a for a in artifacts if a.get("exists")]
    missing = [a for a in artifacts if not a.get("exists")]
    return {
        "created_count": len(existing),
        "missing_count": len(missing),
        "artifacts": existing,
        "missing": missing,
    }


def workspace_file_exists(relative_path: str) -> bool:
    path = (WORKSPACE_DIR / relative_path).resolve()
    root = WORKSPACE_DIR.resolve()
    return root in path.parents and path.exists() and path.is_file()
