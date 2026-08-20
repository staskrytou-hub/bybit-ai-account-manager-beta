from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from artifacts import register_artifact
from journal import log_event
from paths import DATA_DIR, WORKSPACE_DIR
from settings import load_settings

PREVIEW_STATE_FILE = DATA_DIR / "preview_servers.json"
_PREVIEW_SERVERS: dict[str, dict[str, Any]] = {}
_PREVIEW_LOCK = threading.RLock()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def _safe_project_dir(project_folder: str) -> tuple[Path, str]:
    rel = (project_folder or "").strip().replace("\\", "/").strip("/")
    if not rel:
        raise ValueError("A relative project folder inside Bybit AI Manager Workspace is required.")
    path = (WORKSPACE_DIR / rel).resolve()
    root = WORKSPACE_DIR.resolve()
    if path == root or root not in path.parents:
        raise ValueError("Project folder must stay inside Bybit AI Manager Workspace.")
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(f"Workspace project folder was not found: {rel}")
    return path, rel


def _safe_workspace_output(filename: str, default_name: str) -> tuple[Path, str]:
    rel = (filename or default_name).strip().replace("\\", "/").lstrip("/")
    path = (WORKSPACE_DIR / rel).resolve()
    root = WORKSPACE_DIR.resolve()
    if root not in path.parents:
        raise ValueError("Output file must stay inside Bybit AI Manager Workspace.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.relative_to(WORKSPACE_DIR).as_posix()


def _project_key(rel: str) -> str:
    return rel.replace("/", "__")


def _save_preview_state() -> None:
    PREVIEW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    with _PREVIEW_LOCK:
        for key, item in _PREVIEW_SERVERS.items():
            thread = item.get("thread")
            if thread is not None and thread.is_alive():
                payload[key] = {
                    "project_folder": item.get("project_folder", ""),
                    "port": int(item.get("port", 0) or 0),
                    "entry_file": item.get("entry_file", "index.html"),
                    "started_at": int(item.get("started_at", 0) or 0),
                    "process": "BybitAIManager/in-process",
                }
    temp = Path(str(PREVIEW_STATE_FILE) + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(PREVIEW_STATE_FILE)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _find_port(preferred: int, span: int = 30) -> int:
    preferred = max(1024, min(int(preferred), 65535))
    for offset in range(span + 1):
        port = preferred + offset
        if port > 65535:
            break
        if not _port_in_use(port):
            return port
    raise RuntimeError(f"Could not find a free localhost port near {preferred}.")


def summarize_workspace_project(project_folder: str, max_files: int = 250) -> str:
    path, rel = _safe_project_dir(project_folder)
    max_files = max(20, min(int(max_files), 800))
    files = sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.relative_to(path).as_posix())
    rel_files = [p.relative_to(path).as_posix() for p in files]
    important = [name for name in rel_files if name.lower() in {"index.html", "package.json", "requirements.txt", "readme.md"}]
    result = {
        "project_folder": rel,
        "file_count": len(rel_files),
        "important_files": important,
        "sample_files": rel_files[:max_files],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def package_workspace_project(project_folder: str, zip_filename: str = "") -> str:
    project_path, rel = _safe_project_dir(project_folder)
    default_name = f"exports/{Path(rel).name}.zip"
    zip_path, zip_rel = _safe_workspace_output(zip_filename, default_name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(project_path.rglob("*")):
            if item.is_file():
                zf.write(item, arcname=Path(rel).name + "/" + item.relative_to(project_path).as_posix())
    register_artifact(zip_rel, kind="file", source="project_zip", description=f"ZIP package of {rel}")
    log_event("project.package", {"project": rel, "zip": zip_rel})
    return f"Created real ZIP artifact: {zip_rel}"


def export_workspace_project(project_folder: str, destination_path: str, overwrite: bool = True) -> str:
    if not str(destination_path or "").strip():
        raise ValueError("destination_path is required.")
    project_path, rel = _safe_project_dir(project_folder)
    target = Path(str(destination_path).strip()).expanduser()
    if not target.is_absolute():
        target = Path.home() / target
    if target.exists():
        if target.is_file():
            raise ValueError("The destination path points to an existing file, not a folder.")
        if overwrite:
            shutil.rmtree(target)
        else:
            raise FileExistsError(f"Destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project_path, target)
    log_event("project.export", {"project": rel, "destination": str(target)})
    return json.dumps({"project_folder": rel, "exported_to": str(target)}, ensure_ascii=False)


def create_preview_launchers(project_folder: str, entry_file: str = "index.html", port: int = 8000) -> str:
    project_path, rel = _safe_project_dir(project_folder)
    entry = (entry_file or "index.html").strip().lstrip("/")
    port = max(1024, min(int(port), 65535))

    cmd_text = f'''@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set PORT={port}
set ENTRY={entry}
where py >nul 2>&1
if %errorlevel%==0 (
  start "Stan Preview Server" /B py -3 -m http.server %PORT% --bind 127.0.0.1
) else (
  echo Python was not found. Start the preview from Bybit AI Manager instead.
  pause
  exit /b 1
)
timeout /t 2 >nul
start "" "http://127.0.0.1:%PORT%/%ENTRY%"
'''
    sh_text = f'''#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PORT={port}
ENTRY="{entry}"
python3 -m http.server "$PORT" --bind 127.0.0.1 >/tmp/stan_preview_$PORT.log 2>&1 &
sleep 2
python3 - <<'EOF'
import webbrowser
webbrowser.open('http://127.0.0.1:{port}/{entry}')
EOF
'''
    cmd_path = project_path / "launch_local_preview.cmd"
    sh_path = project_path / "launch_local_preview.sh"
    cmd_path.write_text(cmd_text, encoding="utf-8")
    sh_path.write_text(sh_text, encoding="utf-8")
    try:
        sh_path.chmod(0o755)
    except Exception:
        pass
    cmd_rel = cmd_path.relative_to(WORKSPACE_DIR).as_posix()
    sh_rel = sh_path.relative_to(WORKSPACE_DIR).as_posix()
    register_artifact(cmd_rel, kind="file", source="preview_launcher", description=f"Windows preview launcher for {rel}")
    register_artifact(sh_rel, kind="file", source="preview_launcher", description=f"Unix preview launcher for {rel}")
    return json.dumps({"project_folder": rel, "entry_file": entry, "port": port, "launchers": [cmd_rel, sh_rel]}, ensure_ascii=False, indent=2)


def start_local_preview(project_folder: str, entry_file: str = "index.html", port: int = 8000, open_browser: bool = True) -> str:
    """Start an in-process localhost preview server.

    Important for the packaged Windows EXE: this deliberately does NOT run sys.executable.
    In a packaged desktop build, spawning sys.executable would open another application window.
    """
    project_path, rel = _safe_project_dir(project_folder)
    entry = (entry_file or "index.html").strip().lstrip("/")
    settings = load_settings()
    if port <= 0:
        port = int(settings.get("preview_default_port", 8000))
    port = max(1024, min(int(port), 65535))
    key = _project_key(rel)

    with _PREVIEW_LOCK:
        existing = _PREVIEW_SERVERS.get(key)
        if existing:
            thread = existing.get("thread")
            existing_port = int(existing.get("port", 0) or 0)
            if thread is not None and thread.is_alive() and existing_port and _port_in_use(existing_port):
                url = f"http://127.0.0.1:{existing_port}/{entry}"
                if open_browser and bool(settings.get("auto_open_preview_browser", True)):
                    webbrowser.open(url)
                return json.dumps({"status": "already_running", "project_folder": rel, "url": url, "port": existing_port, "server_mode": "in_process"}, ensure_ascii=False)

        chosen_port = _find_port(port)
        handler = functools.partial(_QuietHandler, directory=str(project_path))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", chosen_port), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name=f"StanPreview-{chosen_port}", daemon=True)
        thread.start()
        time.sleep(0.15)
        if not thread.is_alive() or not _port_in_use(chosen_port):
            try:
                server.server_close()
            except Exception:
                pass
            raise RuntimeError("Local preview server failed to start.")
        _PREVIEW_SERVERS[key] = {
            "server": server,
            "thread": thread,
            "project_folder": rel,
            "port": chosen_port,
            "entry_file": entry,
            "started_at": int(time.time()),
        }
        _save_preview_state()

    url = f"http://127.0.0.1:{chosen_port}/{entry}"
    if open_browser and bool(settings.get("auto_open_preview_browser", True)):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    log_event("project.preview.start", {"project": rel, "port": chosen_port, "entry": entry, "server_mode": "in_process"})
    return json.dumps({"status": "started", "project_folder": rel, "url": url, "port": chosen_port, "server_mode": "in_process"}, ensure_ascii=False)


def stop_local_preview(project_folder: str = "") -> str:
    targets: list[tuple[str, dict[str, Any]]] = []
    with _PREVIEW_LOCK:
        if str(project_folder or "").strip():
            _, rel = _safe_project_dir(project_folder)
            key = _project_key(rel)
            item = _PREVIEW_SERVERS.get(key)
            if item:
                targets.append((key, item))
        else:
            targets = list(_PREVIEW_SERVERS.items())

    stopped: list[dict[str, Any]] = []
    for key, item in targets:
        server = item.get("server")
        thread = item.get("thread")
        rel = str(item.get("project_folder", ""))
        port = int(item.get("port", 0) or 0)
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        except Exception:
            pass
        with _PREVIEW_LOCK:
            _PREVIEW_SERVERS.pop(key, None)
        stopped.append({"project_folder": rel, "port": port})
        log_event("project.preview.stop", {"project": rel, "port": port})
    _save_preview_state()
    return json.dumps({"stopped": stopped}, ensure_ascii=False, indent=2)


def deliver_workspace_project(
    project_folder: str,
    destination_path: str = "",
    entry_file: str = "index.html",
    port: int = 8000,
    open_browser: bool = True,
    create_zip: bool = True,
    create_launchers: bool = True,
) -> str:
    _, rel = _safe_project_dir(project_folder)
    result: dict[str, Any] = {"project_folder": rel}
    if create_launchers:
        result["launchers"] = json.loads(create_preview_launchers(project_folder, entry_file=entry_file, port=port))
    if create_zip:
        zip_msg = package_workspace_project(project_folder)
        result["zip_artifact"] = zip_msg.replace("Created real ZIP artifact: ", "")
    if destination_path.strip():
        result["export"] = json.loads(export_workspace_project(project_folder, destination_path))
    result["preview"] = json.loads(start_local_preview(project_folder, entry_file=entry_file, port=port, open_browser=open_browser))
    log_event("project.deliver", {"project": rel, "destination": destination_path, "entry": entry_file})
    return json.dumps(result, ensure_ascii=False, indent=2)
