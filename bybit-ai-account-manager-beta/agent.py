from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from agents import Agent, CodeInterpreterTool, SQLiteSession, WebSearchTool, function_tool

from artifacts import list_artifacts as db_list_artifacts, register_artifact
from context_manager import managed_session
from image_tools import generate_image_to_workspace
from journal import log_event
from local_ops import (
    create_preview_launchers,
    deliver_workspace_project,
    export_workspace_project,
    package_workspace_project,
    start_local_preview,
    stop_local_preview,
    summarize_workspace_project,
)
from memory import save_memory, search_memories
from model_router import choose_model, fallback_models
from paths import CONVERSATION_DB
from resilience import run_sync_resilient
from settings import load_settings
from site_audit import audit_static_site_json
from tasks import create_task as db_create_task
from tasks import list_tasks as db_list_tasks
from tasks import update_task as db_update_task
from usage import record_usage
from workspace import list_workspace_files as fs_list_workspace_files
from workspace import read_text_file as fs_read_text_file
from workspace import write_text_file as fs_write_text_file

ApprovalHandler = Callable[[dict[str, str]], bool]


@function_tool
def remember(category: str, topic: str, content: str, importance: int = 3) -> str:
    """Save durable information to structured local memory.

    Args:
        category: One of profile, preference, rule, project, lesson, reference, other.
        topic: Short searchable label.
        content: Exact useful information to remember.
        importance: 1 to 5, where 5 is highly important for future work.
    """
    memory_id = save_memory(topic, content, category=category, importance=importance)
    log_event("tool.remember", {"memory_id": memory_id, "category": category, "topic": topic})
    return f"Saved memory #{memory_id} in category '{category}'."


@function_tool
def recall(query: str, category: str = "") -> str:
    """Search structured local memory for facts, preferences, rules, projects or lessons."""
    results = search_memories(query, category=category)
    log_event("tool.recall", {"query": query, "category": category, "matches": len(results)})
    return json.dumps(results, ensure_ascii=False, indent=2) if results else "No matching memories found."


@function_tool
def create_task(title: str, details: str = "", priority: str = "normal") -> str:
    """Create a persistent task in Bybit AI Manager's task board."""
    task_id = db_create_task(title, details, priority)
    log_event("tool.task.create", {"task_id": task_id, "title": title, "priority": priority})
    return f"Created task #{task_id}: {title}"


@function_tool
def update_task(task_id: int, status: str, details: str = "") -> str:
    """Update a persistent task status or details."""
    changed = db_update_task(task_id, status, details)
    log_event("tool.task.update", {"task_id": task_id, "status": status, "changed": changed})
    return f"Task #{task_id} updated to {status}." if changed else f"Task #{task_id} was not found."


@function_tool
def get_tasks(status: str = "") -> str:
    """List persistent tasks, optionally filtering by status."""
    items = db_list_tasks(status=status, limit=50)
    log_event("tool.task.list", {"status": status, "matches": len(items)})
    return json.dumps(items, ensure_ascii=False, indent=2) if items else "No tasks found."


@function_tool
def list_workspace_files() -> str:
    """List files inside the isolated Bybit AI Manager Workspace folder."""
    files = fs_list_workspace_files(limit=500)
    log_event("tool.workspace.list", {"count": len(files)})
    return json.dumps(files, ensure_ascii=False)


@function_tool
def read_workspace_file(filename: str) -> str:
    """Read a UTF-8 text file from the isolated Bybit AI Manager Workspace folder."""
    text = fs_read_text_file(filename)
    log_event("tool.workspace.read", {"filename": filename, "chars": len(text)})
    return text


@function_tool
def write_workspace_file(filename: str, content: str) -> str:
    """Create or replace a UTF-8 text file inside Bybit AI Manager Workspace. This is trusted and needs no confirmation."""
    relative = fs_write_text_file(filename, content)
    register_artifact(relative, kind="file", source="workspace_write")
    log_event("tool.workspace.write", {"filename": relative, "chars": len(content)})
    return f"Wrote real workspace file: {relative}"


@function_tool
def generate_workspace_image(
    prompt: str,
    filename: str,
    size: str = "1536x1024",
    quality: str = "auto",
    output_format: str = "png",
) -> str:
    """Generate a REAL image with GPT Image 2 and save it to Bybit AI Manager Workspace.

    Use this whenever the user asks to create/generate photos, illustrations, visual assets,
    dashboard illustrations, chart visuals, icons, logos, backgrounds, or other original imagery.
    Do not substitute SVG placeholders when this tool can create the requested image.

    Args:
        prompt: Detailed description of the desired image.
        filename: Relative Workspace path such as TradingProject/assets/images/dashboard-01.webp.
        size: 1024x1024, 1024x1536, 1536x1024, or auto.
        quality: low, medium, high, or auto. auto uses the user setting.
        output_format: png, jpeg, or webp.
    """
    relative = generate_image_to_workspace(prompt, filename, size=size, quality=quality, output_format=output_format)
    return f"Generated real image artifact: {relative}"


@function_tool
def get_recent_artifacts(limit: int = 30) -> str:
    """List real files/images registered in Bybit AI Manager Workspace with existence and size evidence."""
    items = db_list_artifacts(after_id=0, limit=max(1, min(int(limit), 100)))
    return json.dumps(
        [
            {
                "path": item["relative_path"],
                "kind": item["kind"],
                "exists": item["exists"],
                "size_bytes": item["size_bytes"],
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    )




@function_tool
def inspect_workspace_project(project_folder: str, max_files: int = 250) -> str:
    """Inspect a real Workspace project folder and summarize what already exists."""
    summary = summarize_workspace_project(project_folder, max_files=max_files)
    log_event("tool.project.inspect", {"project_folder": project_folder})
    return summary


@function_tool
def zip_workspace_project(project_folder: str, zip_filename: str = "") -> str:
    """Create a real ZIP artifact for a Workspace project folder."""
    result = package_workspace_project(project_folder, zip_filename=zip_filename)
    log_event("tool.project.zip", {"project_folder": project_folder, "zip_filename": zip_filename})
    return result


@function_tool
def export_project_to_disk(project_folder: str, destination_path: str, overwrite: bool = True) -> str:
    """Export a Workspace project folder to a real local destination path, such as D:\\ExampleProject."""
    result = export_workspace_project(project_folder, destination_path, overwrite=overwrite)
    log_event("tool.project.export", {"project_folder": project_folder, "destination_path": destination_path})
    return result


@function_tool
def create_project_preview_launchers(project_folder: str, entry_file: str = "index.html", port: int = 8000) -> str:
    """Create one-click local preview launch scripts inside a Workspace project."""
    result = create_preview_launchers(project_folder, entry_file=entry_file, port=port)
    log_event("tool.project.preview_launchers", {"project_folder": project_folder, "entry_file": entry_file, "port": port})
    return result


@function_tool
def run_local_preview(project_folder: str, entry_file: str = "index.html", port: int = 0, open_browser: bool = True) -> str:
    """Start a real localhost preview server for a Workspace project and return the actual URL."""
    result = start_local_preview(project_folder, entry_file=entry_file, port=port, open_browser=open_browser)
    log_event("tool.project.preview_start", {"project_folder": project_folder, "entry_file": entry_file, "port": port})
    return result


@function_tool
def stop_project_preview(project_folder: str = "") -> str:
    """Stop a local preview server previously started for a Workspace project."""
    result = stop_local_preview(project_folder)
    log_event("tool.project.preview_stop", {"project_folder": project_folder})
    return result


@function_tool
def audit_project_site(project_folder: str, entry_file: str = "index.html", require_local_images: bool = False) -> str:
    """Audit a static website/app project before claiming it is complete.

    Checks the real entry file, local image assets, broken img/script/css references, image file signatures,
    and placeholder references. Set require_local_images=True when the user explicitly requested real photos/images.
    """
    result = audit_static_site_json(project_folder, entry_file=entry_file, require_local_images=require_local_images)
    log_event("tool.project.audit", {"project_folder": project_folder, "entry_file": entry_file, "require_local_images": require_local_images})
    return result


@function_tool
def deliver_project(project_folder: str, destination_path: str = "", entry_file: str = "index.html", port: int = 0, open_browser: bool = True, create_zip: bool = True, create_launchers: bool = True) -> str:
    """Prepare a Workspace project for real local use: ZIP, launchers, optional export to disk, and actual localhost preview."""
    result = deliver_workspace_project(project_folder, destination_path=destination_path, entry_file=entry_file, port=port, open_browser=open_browser, create_zip=create_zip, create_launchers=create_launchers)
    log_event("tool.project.deliver", {"project_folder": project_folder, "destination_path": destination_path, "entry_file": entry_file, "port": port})
    return result




@function_tool
def get_bybit_futures_snapshot(symbol: str = "BTCUSDT", interval: str = "15") -> str:
    """Get a REAL current Bybit linear-futures market snapshot with deterministic indicators.

    Use this for futures/market analysis instead of guessing prices or indicators. This tool does not place orders.
    """
    from market_analysis import build_market_snapshot
    snapshot = build_market_snapshot(symbol.upper(), interval=interval, testnet_market_data=False)
    log_event("tool.trading.snapshot", {"symbol": symbol.upper(), "interval": interval})
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


@function_tool
def get_trading_core_status() -> str:
    """Return Stan Trading Core runtime status, latest market analysis, hard-risk result and paper/Testnet execution state."""
    from trading_engine import TRADING_CONTROLLER
    status = TRADING_CONTROLLER.status()
    return json.dumps(status, ensure_ascii=False, indent=2, default=str)

MAIN_INSTRUCTIONS = (
    "You are Bybit AI Manager, an execution-oriented assistant for Bybit account workflows. Reply in Ukrainian by default unless the user asks otherwise. "
    "Your job is to DO the requested work with available tools, not merely explain what could be done.\n\n"
    "EXECUTION DISCIPLINE:\n"
    "1. Treat the user's wording as a contract. Extract all explicit deliverables, exact counts, names, formats, features, constraints, exclusions and completion conditions.\n"
    "2. Preserve exact quantities. 50 means 50. Six symbols means six. Never silently replace finished work with examples, placeholders or a smaller demo.\n"
    "3. If a tool can perform the requested action, use it now. Do not stop at a plan or ask 'shall I proceed?' when the request is sufficiently specified.\n"
    "4. For existing Workspace projects, inspect the real files first, preserve working parts, then edit the actual project. Use inspect_workspace_project/list_workspace_files/read_workspace_file as evidence.\n"
    "5. For requested original images/photos/design assets, call generate_workspace_image and create REAL image files. Do not pretend SVG/CSS placeholders are generated photos.\n"
    "6. Never invent a download URL or claim a file exists. Create the artifact first. The desktop app will expose real local Open buttons for registered artifacts.\n"
    "7. Use web search for current/external/uncertain facts. Use Code Interpreter for calculations, data analysis, code execution and independent checks.\n"
    "8. Before saying 'done', verify deliverables against the original request. Use list_workspace_files/get_recent_artifacts/read_workspace_file as evidence and fix achievable omissions.\n"
    "9. If a tool fails transiently, retry or use an alternative tool/path. If a capability truly does not exist, finish everything else and identify only the exact blocker.\n"
    "10. Never claim an external action happened unless a tool actually performed it.\n\n"
    "TRADING DISCIPLINE:\n"
    "- For futures/crypto market questions, never invent current price, funding, open interest, RSI, spread or account state. Use get_bybit_futures_snapshot/get_trading_core_status when relevant.\n"
    "- The dedicated Trading Core owns autonomous market monitoring, Paper/Shadow/Testnet execution and hard risk limits. The general chat agent must not claim a trade was placed unless Trading Core reports it.\n"
    "- Do not bypass Trading Core risk limits or present uncertain analysis as guaranteed profit.\n\n"
    "PROJECT DELIVERY PLAYBOOK:\n"
    "- When the user asks to review what was already created, inspect the real Workspace project first.\n"
    "- When the user asks to save/export the project to disk or to a path like D:\\..., use export_project_to_disk or deliver_project.\n"
    "- When the user asks for a ZIP, create a real ZIP artifact with zip_workspace_project or deliver_project.\n"
    "- When the user asks to run the project locally and provide a link, use run_local_preview or deliver_project and return the ACTUAL localhost URL from the tool result.\n"
    "- When a static site/app needs one-click launch files, create real launcher scripts with create_project_preview_launchers or deliver_project.\n"
    "- WEBSITE COMPLETION GATE: before saying a website is finished, call audit_project_site. If the user asked for photos/images, pass require_local_images=True. A failed audit means the task is NOT done: fix broken references, missing images, invalid image files or placeholders and audit again.\n"
    "- Never claim generated images are visible on the site merely because image files exist. The HTML/CSS must actually reference them, and audit_project_site must show referenced_local_image_count > 0 when images were requested.\n\n"
    "LEARNING: High-priority user corrections and durable lessons may be injected into context. Follow them as operating rules unless the current user instruction overrides them.\n\n"
    "BOUNDARIES: Workspace writes, project packaging/export, launcher creation, and localhost preview are allowed tools. You still do not have unrestricted Windows/PowerShell, account logins, email sending, purchases or arbitrary app control unless explicit tools are added later."
)


def _tools() -> list[Any]:
    return [
        WebSearchTool(search_context_size="medium"),
        CodeInterpreterTool(tool_config={"type": "code_interpreter", "container": {"type": "auto"}}),
        remember,
        recall,
        create_task,
        update_task,
        get_tasks,
        list_workspace_files,
        read_workspace_file,
        write_workspace_file,
        generate_workspace_image,
        get_recent_artifacts,
        inspect_workspace_project,
        zip_workspace_project,
        export_project_to_disk,
        create_project_preview_launchers,
        run_local_preview,
        stop_project_preview,
        audit_project_site,
        deliver_project,
        get_bybit_futures_snapshot,
        get_trading_core_status,
    ]


def build_agent(model: str) -> Agent:
    return Agent(name="Bybit AI Manager", model=model, instructions=MAIN_INSTRUCTIONS, tools=_tools())


def usage_summary(result: Any) -> dict[str, int]:
    usage = result.context_wrapper.usage
    return {
        "requests": int(usage.requests),
        "input_tokens": int(usage.input_tokens),
        "output_tokens": int(usage.output_tokens),
        "total_tokens": int(usage.total_tokens),
    }


def add_usage(*items: dict[str, int]) -> dict[str, int]:
    keys = ("requests", "input_tokens", "output_tokens", "total_tokens")
    return {key: sum(int(item.get(key, 0)) for item in items) for key in keys}


def _is_model_unavailable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in {400, 404} and "model" in text and any(x in text for x in ("not found", "does not exist", "access", "permission", "available"))


def _run_with_approvals(
    agent: Agent,
    task: str,
    *,
    session: Any | None,
    max_turns: int,
    approval_handler: ApprovalHandler | None,
) -> Any:
    result = run_sync_resilient(agent, task, session=session, max_turns=max_turns, kind="agent")
    while result.interruptions:
        state = result.to_state()
        for interruption in result.interruptions:
            info = {
                "tool": str(interruption.name or "unknown_tool"),
                "arguments": str(interruption.arguments or ""),
                "agent": str(getattr(getattr(interruption, "agent", None), "name", "Bybit AI Manager")),
            }
            log_event("approval.requested", info)
            approved = bool(approval_handler(info)) if approval_handler is not None else False
            if approved:
                state.approve(interruption, always_approve=False)
                log_event("approval.approved", info)
            else:
                state.reject(interruption, rejection_message="The user rejected this action. Continue safely without it.")
                log_event("approval.rejected", info)
        result = run_sync_resilient(agent, state, session=session, max_turns=max_turns, kind="agent.resume")
    return result


def _durable_lessons() -> str:
    items = search_memories("", category="lesson", limit=8)
    if not items:
        return ""
    lines = [f"- {item['content']}" for item in items if str(item.get("content", "")).strip()]
    return "\n".join(lines[:8])


def run_agent_once(
    task: str,
    *,
    session_id: str,
    max_turns: int = 12,
    approval_handler: ApprovalHandler | None = None,
    log_kind: str = "run",
    model: str | None = None,
    autonomous: bool = False,
) -> tuple[str, dict[str, int]]:
    task = task.strip()
    if not task:
        raise ValueError("Task cannot be empty.")
    selection = choose_model(task, autonomous=autonomous, role="worker")
    preferred = model or selection.model
    lessons = _durable_lessons()
    enriched = task
    if lessons:
        enriched = f"[HIGH-PRIORITY LEARNED LESSONS]\n{lessons}\n[/HIGH-PRIORITY LEARNED LESSONS]\n\n{task}"

    log_event(f"{log_kind}.start", {"task": task[:1000], "session_id": session_id, "preferred_model": preferred})
    cfg = load_settings()
    session = managed_session(session_id) if bool(cfg.get("use_sdk_session_history", False)) else None
    last_exc: BaseException | None = None
    used_model = preferred
    for candidate in fallback_models(preferred):
        used_model = candidate
        try:
            result = _run_with_approvals(
                build_agent(candidate),
                enriched,
                session=session,
                max_turns=max_turns,
                approval_handler=approval_handler,
            )
            usage = usage_summary(result)
            record_usage(usage)
            log_event(f"{log_kind}.finish", {"usage": usage, "session_id": session_id, "model": candidate})
            return str(result.final_output), usage
        except Exception as exc:
            last_exc = exc
            if not _is_model_unavailable(exc):
                raise
            log_event("model.fallback", {"from": candidate, "error": f"{type(exc).__name__}: {exc}"[:1200]})
            continue
    raise RuntimeError(f"No configured model was available. Last error: {last_exc}")


def append_session_turn(session_id: str, user_text: str, assistant_text: str) -> None:
    if not bool(load_settings().get("use_sdk_session_history", False)):
        return
    session = SQLiteSession(session_id, CONVERSATION_DB)
    asyncio.run(session.add_items([{"role": "user", "content": user_text}, {"role": "assistant", "content": assistant_text}]))


def run_task(
    task: str,
    approval_handler: ApprovalHandler | None = None,
    *,
    session_id: str = "main",
    context_note: str = "",
    model: str | None = None,
) -> tuple[str, dict[str, int]]:
    prompt = task.strip()
    if context_note.strip():
        prompt = f"[LOCAL APP CONTEXT]\n{context_note.strip()}\n[/LOCAL APP CONTEXT]\n\n{prompt}"
    return run_agent_once(
        prompt,
        session_id=session_id,
        max_turns=16,
        approval_handler=approval_handler,
        log_kind="run",
        model=model,
        autonomous=False,
    )
