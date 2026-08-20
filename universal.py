from __future__ import annotations

from collections.abc import Callable
import re

from agent import run_task
from artifacts import artifact_watermark
from journal import log_event
from learning import capture_user_correction
from model_router import choose_model
from settings import load_settings
from verification import verify_artifacts

_MULTI_ACTION = re.compile(
    r"\b(створ|зроб|побуд|розроб|перероб|дороб|виправ|мігру|рефактор|сайт|website|app\b|додат|проєкт|project|"
    r"автоматиз|дослід|збери|проаналізуй|дизайн|фото|картин|запусти|збережи|переглянь|оформ|implement|build|fix|debug|migrate)\w*",
    re.IGNORECASE,
)
_EXACT_FEATURES = re.compile(r"\b\d+\b|\b(trading|торгів|order|ордер|position|позиці|risk|ризик|market|ринок|api|database|база даних|image|фото|картин)\w*", re.IGNORECASE)


def _route(request: str) -> tuple[str, str]:
    text = (request or "").strip()
    score = 0
    if _MULTI_ACTION.search(text):
        score += 2
    if len(text) > 900:
        score += 1
    if len(_EXACT_FEATURES.findall(text)) >= 3:
        score += 2
    if len(re.findall(r"(?:^|\n)\s*[-*\d]+[.)-]?\s+", text)) >= 3:
        score += 1
    lower = text.lower()
    if any(word in lower for word in ("повністю", "під ключ", "end-to-end", "весь проєкт", "всі файли")):
        score += 2
    project_execution = any(word in lower for word in ("сайт", "website", "проєкт", "project", "дизайн", "фото", "картин", "workspace", "локально", "localhost", "диск d", "збережи його", "запусти його")) and any(word in lower for word in ("зроб", "дороб", "виправ", "створ", "запуст", "збереж", "додай", "перероб", "переглянь"))
    if project_execution:
        score = max(score, 3)
    mode = "autonomous" if score >= 3 else "direct"
    return mode, f"deterministic execution score={score}"


def run_universal(
    request: str,
    *,
    approval_handler: Callable[[dict[str, str]], bool] | None = None,
    progress_handler: Callable[[dict[str, object]], None] | None = None,
    stop_event: object | None = None,
    session_id: str = "main",
    project_name: str = "General",
    conversation_context: str = "",
) -> tuple[str, dict[str, int], dict[str, object]]:
    cfg = load_settings()
    if bool(cfg.get("auto_learn_corrections", True)):
        try:
            capture_user_correction(request)
        except Exception as exc:
            log_event("learning.capture_failed", {"error": f"{type(exc).__name__}: {exc}"})

    mode, reason = _route(request)
    if progress_handler:
        progress_handler({"type": "route", "mode": mode, "reason": reason})

    if mode == "autonomous":
        from autonomy import run_autonomous
        answer, task_usage, meta = run_autonomous(
            request,
            approval_handler=approval_handler,
            progress_handler=progress_handler,
            stop_event=stop_event,
            session_id=session_id,
            project_name=project_name,
            conversation_context=conversation_context,
        )
        return answer, task_usage, {"mode": "autonomous", "route_reason": reason, **meta}

    start_artifact_id = artifact_watermark()
    context_note = (
        f"Active project: {project_name}. Continue the current saved chat thread. Execute the user's request concretely and preserve every explicit requirement. "
        "Never invent a download link: create a real Workspace artifact instead."
    )
    if conversation_context.strip():
        context_note += "\n\nCOMPACT PROJECT/CHAT CONTINUITY:\n" + conversation_context.strip()
    selection = choose_model(request, autonomous=False, role="worker")
    answer, direct_usage = run_task(
        request,
        approval_handler=approval_handler,
        session_id=session_id,
        context_note=context_note,
        model=selection.model,
    )
    artifact_evidence = verify_artifacts(start_artifact_id)
    return answer, direct_usage, {
        "mode": "direct",
        "route_reason": reason,
        "model": selection.model,
        "artifacts": artifact_evidence["artifacts"],
    }
