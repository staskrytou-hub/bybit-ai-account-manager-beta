from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from agents import Agent
from pydantic import BaseModel, Field

from agent import add_usage, append_session_turn, run_agent_once, usage_summary
from artifacts import artifact_watermark
from autonomy_store import (
    add_run_usage,
    autonomous_usage_today,
    create_run,
    get_run,
    set_run_plan,
    set_run_status,
    update_step,
)
from journal import log_event
from memory import save_memory
from model_router import choose_model, fallback_models
from resilience import run_sync_resilient
from settings import load_settings
from site_audit import audit_static_site
from paths import WORKSPACE_DIR
from usage import record_usage
from verification import verify_artifacts

ProgressHandler = Callable[[dict[str, object]], None]
ApprovalHandler = Callable[[dict[str, str]], bool]


class PlanStep(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)


class AutonomousPlan(BaseModel):
    requirements: list[str] = Field(default_factory=list, max_length=40)
    success_criteria: str = Field(min_length=1, max_length=2400)
    steps: list[PlanStep]


class StepAssessment(BaseModel):
    passed: bool
    summary: str = Field(min_length=1, max_length=1800)
    evidence: str = Field(default="", max_length=2400)
    missing_requirements: list[str] = Field(default_factory=list, max_length=20)
    lesson: str = Field(default="", max_length=1200)
    retry_guidance: str = Field(default="", max_length=1400)


PLANNER_INSTRUCTIONS = (
    "Turn the user's goal into an executable plan. The user's wording is a contract. Extract every explicit deliverable, exact "
    "count, name, feature, file, format, language, constraint, exclusion and completion condition. Never reduce counts to examples. "
    "Plan the smallest sequence that can actually produce and verify the result with Stan's available tools. For websites/apps/projects, "
    "include implementation and a final acceptance/fix step. If the user asks for images/photos, explicitly require real generated image "
    "files, not placeholders. For website work, the plan must include a real static-site audit after implementation and after fixes; "
    "if photos/images were requested, the audit must verify that local image files are actually referenced by HTML/CSS. Return structured requirements, success criteria and steps only."
)

JUDGE_INSTRUCTIONS = (
    "Judge the execution report against the CURRENT STEP and all relevant explicit user requirements. Be strict and evidence-based. "
    "A claimed file/image is not evidence unless the report/tool evidence shows it exists. Exact counts must match. Placeholders do not "
    "satisfy a request for finished assets. For websites, a passed audit_project_site result is required before accepting completion; when images were requested, "
    "the audit must show real local images are referenced by the site. Fail when an achievable requirement is missing and provide concrete retry guidance."
)

FINALIZER_INSTRUCTIONS = (
    "Produce the final user-facing result from verified run evidence. State what was actually completed and where real artifacts are. "
    "Do not invent URLs, downloads, files or actions. If artifacts are listed as existing, refer to their Workspace paths and let the "
    "desktop app provide Open links. Mention remaining blockers only if they are real."
)


def _emit(handler: ProgressHandler | None, **event: object) -> None:
    if handler:
        handler(event)


def _is_stopped(stop_event: object | None) -> bool:
    return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())


def _looks_like_website_goal(goal: str) -> bool:
    return bool(re.search(r"\b(сайт|website|html|css|frontend|лендинг|landing|веб)\w*", goal or "", re.IGNORECASE))


def _goal_requires_images(goal: str) -> bool:
    return bool(re.search(r"\b(фото|фотограф|картин|зображ|image|photo|picture|gallery|галере)\w*", goal or "", re.IGNORECASE))


def _infer_site_folder(project_name: str, artifacts: list[dict[str, object]]) -> str | None:
    candidates: list[str] = []
    for item in artifacts:
        rel = str(item.get("relative_path", "")).replace("\\", "/").strip("/")
        if "/" in rel:
            candidates.append(rel.split("/", 1)[0])
    if project_name and project_name.lower() != "general":
        candidates.insert(0, project_name.replace(" ", ""))
        candidates.insert(0, project_name)
    for child in WORKSPACE_DIR.iterdir():
        if child.is_dir() and (child / "index.html").exists():
            candidates.append(child.name)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        folder = WORKSPACE_DIR / candidate
        if folder.is_dir() and (folder / "index.html").exists():
            return candidate
    return None


def _zero_usage() -> dict[str, int]:
    return {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _is_model_unavailable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in {400, 404} and "model" in text and any(x in text for x in ("not found", "does not exist", "access", "permission", "available"))


def _model_run(
    *,
    name: str,
    instructions: str,
    output_type: type[BaseModel] | None,
    prompt: str,
    preferred_model: str,
    max_turns: int,
    kind: str,
) -> tuple[Any, dict[str, int], str]:
    last_exc: BaseException | None = None
    for model in fallback_models(preferred_model):
        agent = Agent(name=name, model=model, instructions=instructions, output_type=output_type) if output_type else Agent(name=name, model=model, instructions=instructions)
        try:
            log_event(f"autonomy.{kind}.start", {"prompt": prompt[:1200], "model": model})
            result = run_sync_resilient(agent, prompt, max_turns=max_turns, kind=f"autonomy.{kind}")
            usage = usage_summary(result)
            record_usage(usage)
            log_event(f"autonomy.{kind}.finish", {"usage": usage, "model": model})
            return result.final_output, usage, model
        except Exception as exc:
            last_exc = exc
            if not _is_model_unavailable(exc):
                raise
            log_event("model.fallback", {"kind": kind, "from": model, "error": f"{type(exc).__name__}: {exc}"[:1200]})
    raise RuntimeError(f"No model available for {kind}. Last error: {last_exc}")


def run_autonomous(
    goal: str,
    *,
    approval_handler: ApprovalHandler | None = None,
    progress_handler: ProgressHandler | None = None,
    stop_event: object | None = None,
    session_id: str = "main",
    project_name: str = "General",
    conversation_context: str = "",
) -> tuple[str, dict[str, int], dict[str, object]]:
    settings = load_settings()
    max_steps = int(settings["autonomy_max_steps"])
    token_budget = int(settings["autonomy_token_budget"])
    daily_budget = int(settings["autonomy_daily_token_budget"])
    retry_limit = int(settings["autonomy_retry_limit"])
    planner_turns = int(settings["autonomy_planner_max_turns"])
    step_turns = int(settings["autonomy_step_max_turns"])

    run_id = create_run(goal, max_steps, token_budget, retry_limit)
    start_artifact_id = artifact_watermark()
    total_usage = _zero_usage()
    log_event("autonomy.run.start", {"run_id": run_id, "goal": goal, "settings": settings})
    _emit(progress_handler, type="planning", run_id=run_id, text="Planning exact requirements...")

    if autonomous_usage_today() >= daily_budget:
        reason = f"Daily autonomous token budget ({daily_budget:,}) is already reached. Increase it in Settings or continue in Chat mode."
        set_run_status(run_id, "budget_exceeded", stop_reason=reason)
        return reason, total_usage, {"run_id": run_id, "status": "budget_exceeded", "artifacts": []}

    try:
        if _is_stopped(stop_event):
            set_run_status(run_id, "stopped", stop_reason="Stopped before planning.")
            return "Autonomous run stopped before planning.", total_usage, {"run_id": run_id, "status": "stopped", "artifacts": []}

        prior_context = conversation_context.strip() or "No prior visible chat context was supplied."
        plan_prompt = (
            f"ACTIVE PROJECT: {project_name}\n\nRECENT CHAT CONTEXT:\n{prior_context}\n\nUSER GOAL:\n{goal}\n\n"
            f"Maximum implementation steps available: {max_steps}. Extract exact requirements and plan execution."
        )
        planner_model = choose_model(goal, autonomous=True, role="planner").model
        plan_output, planner_usage, used_planner_model = _model_run(
            name="Bybit AI Manager Planner",
            instructions=PLANNER_INSTRUCTIONS,
            output_type=AutonomousPlan,
            prompt=plan_prompt,
            preferred_model=planner_model,
            max_turns=planner_turns,
            kind="planner",
        )
        total_usage = add_usage(total_usage, planner_usage)
        add_run_usage(run_id, planner_usage)
        if not isinstance(plan_output, AutonomousPlan):
            raise TypeError("Planner returned an unexpected output type")

        steps = list(plan_output.steps[:max_steps])
        if not steps:
            steps = [PlanStep(title="Execute request", objective="Execute and verify the complete user request.")]
        if len(steps) < max_steps and not any("accept" in s.title.lower() or "verify" in s.title.lower() or "перев" in s.title.lower() for s in steps):
            steps.append(
                PlanStep(
                    title="Final acceptance and repair",
                    objective=(
                        "Compare real outputs/artifacts against EVERY explicit original requirement. Inspect Workspace/artifact evidence, "
                        "fix every achievable omission, and do not finish with placeholders or merely list missing work."
                    ),
                )
            )
        steps = steps[:max_steps]
        set_run_plan(run_id, plan_output.success_criteria, [step.model_dump() for step in steps])
        _emit(progress_handler, type="plan", run_id=run_id, steps=[step.title for step in steps], model=used_planner_model)

        requirements = [str(item).strip() for item in plan_output.requirements if str(item).strip()] or [goal.strip()]
        requirements_text = "\n".join(f"- {item}" for item in requirements[:40])
        previous_reports: list[str] = []
        lessons: list[str] = []
        stopped_reason = ""
        final_status = "completed"
        worker_model = choose_model(goal, autonomous=True, role="worker").model
        judge_model = choose_model(goal, autonomous=True, role="judge").model

        for step_no, step in enumerate(steps, start=1):
            if _is_stopped(stop_event):
                stopped_reason = "Stopped by user before the next step."
                final_status = "stopped"
                update_step(run_id, step_no, status="stopped")
                break
            if total_usage["total_tokens"] >= token_budget:
                stopped_reason = f"Per-task token budget ({token_budget:,}) reached."
                final_status = "budget_exceeded"
                update_step(run_id, step_no, status="stopped")
                break
            if autonomous_usage_today() >= daily_budget:
                stopped_reason = f"Daily autonomous token budget ({daily_budget:,}) reached."
                final_status = "budget_exceeded"
                update_step(run_id, step_no, status="stopped")
                break

            passed = False
            retry_guidance = ""
            for attempt in range(1, retry_limit + 2):
                if _is_stopped(stop_event):
                    stopped_reason = "Stopped by user."
                    final_status = "stopped"
                    update_step(run_id, step_no, status="stopped", attempt=attempt - 1)
                    break

                status = "retrying" if attempt > 1 else "in_progress"
                update_step(run_id, step_no, status=status, attempt=attempt)
                _emit(progress_handler, type="step_start", run_id=run_id, step_no=step_no, total_steps=len(steps), title=step.title, attempt=attempt, model=worker_model)
                context = "\n\n".join(previous_reports[-5:]) or "No previous verified step reports."
                step_prompt = (
                    f"AUTONOMOUS RUN #{run_id}\nACTIVE PROJECT: {project_name}\nOVERALL GOAL:\n{goal}\n\n"
                    f"EXPLICIT USER REQUIREMENTS (hard acceptance criteria):\n{requirements_text}\n\n"
                    f"SUCCESS CRITERIA:\n{plan_output.success_criteria}\n\nCURRENT STEP {step_no}/{len(steps)}: {step.title}\n"
                    f"OBJECTIVE:\n{step.objective}\n\nPREVIOUS VERIFIED REPORTS:\n{context}\n\n"
                    + (f"RETRY GUIDANCE:\n{retry_guidance}\n\n" if retry_guidance else "")
                    + "Execute this step NOW with tools. Preserve exact counts/names/features. Inspect existing Workspace files before editing. "
                    "If images/photos are required, generate REAL image files with generate_workspace_image AND wire them into the site's HTML/CSS. Never invent download links. "
                    "For website work, call audit_project_site before reporting success; if the audit fails, fix the project and audit again. "
                    "Before reporting, verify actual files/artifacts exist. Return concise evidence including exact paths, audit result and counts."
                )

                def guarded_approval(info: dict[str, str]) -> bool:
                    if _is_stopped(stop_event):
                        return False
                    return bool(approval_handler(info)) if approval_handler else False

                report, step_usage = run_agent_once(
                    step_prompt,
                    session_id=f"autonomy-{run_id}-step-{step_no}-attempt-{attempt}",
                    max_turns=step_turns,
                    approval_handler=guarded_approval,
                    log_kind="autonomy.step",
                    model=worker_model,
                    autonomous=True,
                )
                total_usage = add_usage(total_usage, step_usage)
                add_run_usage(run_id, step_usage)
                update_step(run_id, step_no, status=status, usage=step_usage)

                if total_usage["total_tokens"] >= token_budget:
                    stopped_reason = f"Per-task token budget ({token_budget:,}) reached after step execution."
                    final_status = "budget_exceeded"
                    update_step(run_id, step_no, status="stopped", summary=report)
                    break

                artifact_evidence = verify_artifacts(start_artifact_id)
                paths = [str(a.get("relative_path")) for a in artifact_evidence["artifacts"][:100]]
                assessment_prompt = (
                    f"OVERALL GOAL:\n{goal}\n\nEXPLICIT USER REQUIREMENTS:\n{requirements_text}\n\n"
                    f"STEP OBJECTIVE:\n{step.objective}\n\nEXECUTION REPORT:\n{report}\n\n"
                    f"REAL ARTIFACT EVIDENCE CREATED/UPDATED THIS RUN:\n{paths or ['none registered']}\n\n"
                    "Judge this step only. Require concrete evidence for claimed work."
                )
                assessment_output, judge_usage, _ = _model_run(
                    name="Bybit AI Manager Step Judge",
                    instructions=JUDGE_INSTRUCTIONS,
                    output_type=StepAssessment,
                    prompt=assessment_prompt,
                    preferred_model=judge_model,
                    max_turns=3,
                    kind="judge",
                )
                total_usage = add_usage(total_usage, judge_usage)
                add_run_usage(run_id, judge_usage)
                update_step(run_id, step_no, status=status, usage=judge_usage)
                if not isinstance(assessment_output, StepAssessment):
                    raise TypeError("Step judge returned an unexpected output type")

                evidence = assessment_output.evidence
                if assessment_output.missing_requirements:
                    evidence += "\nMissing: " + "; ".join(assessment_output.missing_requirements)
                update_step(
                    run_id,
                    step_no,
                    status="passed" if assessment_output.passed else ("retrying" if attempt <= retry_limit else "failed"),
                    summary=assessment_output.summary,
                    evidence=evidence,
                    lesson=assessment_output.lesson,
                )
                if assessment_output.lesson.strip():
                    lessons.append(assessment_output.lesson.strip())

                if assessment_output.passed:
                    previous_reports.append(f"Step {step_no} - {step.title}: {assessment_output.summary}\nEvidence: {evidence}")
                    passed = True
                    _emit(progress_handler, type="step_passed", run_id=run_id, step_no=step_no, title=step.title)
                    break

                retry_guidance = assessment_output.retry_guidance or "Fix the missing requirement(s), produce evidence, and retry."
                _emit(progress_handler, type="step_retry", run_id=run_id, step_no=step_no, title=step.title, attempt=attempt)

            if final_status in {"stopped", "budget_exceeded"}:
                break
            if not passed:
                final_status = "blocked"
                stopped_reason = f"Step {step_no} could not be verified after {retry_limit + 1} attempt(s)."
                break

        if final_status == "completed" and len(previous_reports) < len(steps):
            final_status = "blocked"
            stopped_reason = "The run ended before all planned steps were verified."

        artifact_evidence = verify_artifacts(start_artifact_id)

        site_audit_result: dict[str, object] | None = None
        if _looks_like_website_goal(goal):
            site_folder = _infer_site_folder(project_name, artifact_evidence["artifacts"])
            if site_folder is None:
                final_status = "blocked"
                stopped_reason = "Website completion gate failed: no real Workspace project with index.html could be identified."
            else:
                try:
                    site_audit_result = audit_static_site(site_folder, "index.html", require_local_images=_goal_requires_images(goal))
                    if not bool(site_audit_result.get("passed")):
                        final_status = "blocked"
                        stopped_reason = (
                            "Website completion gate failed. The real static-site audit found unresolved issues: "
                            + str(site_audit_result)[:1800]
                        )
                except Exception as audit_exc:
                    final_status = "blocked"
                    stopped_reason = f"Website completion gate could not verify the real site: {type(audit_exc).__name__}: {audit_exc}"

        artifact_lines = [
            f"- {a['relative_path']} ({a['kind']}, {a['size_bytes']} bytes, exists={a['exists']})"
            for a in artifact_evidence["artifacts"][:200]
        ]
        synthesis_prompt = (
            f"USER GOAL:\n{goal}\n\nRUN STATUS: {final_status}\nSTOP/BLOCK REASON: {stopped_reason or 'none'}\n\n"
            f"EXPLICIT USER REQUIREMENTS:\n{requirements_text}\n\nSUCCESS CRITERIA:\n{plan_output.success_criteria}\n\n"
            f"VERIFIED STEP REPORTS:\n{(' '.join(previous_reports) if previous_reports else 'No step was fully verified.')}\n\n"
            f"REAL ARTIFACTS REGISTERED THIS RUN:\n{chr(10).join(artifact_lines) if artifact_lines else 'none'}\n\n"
            f"PROGRAMMATIC STATIC-SITE AUDIT:\n{site_audit_result if site_audit_result is not None else 'not applicable'}\n\n"
            f"TOKEN USAGE SO FAR: {total_usage['total_tokens']:,}\n"
            "Return the concrete final result. Do not invent URLs. Refer only to real artifact paths listed above."
        )
        final_model = choose_model(goal, autonomous=True, role="finalizer").model
        final_output, final_usage, used_final_model = _model_run(
            name="Bybit AI Manager Result Synthesizer",
            instructions=FINALIZER_INSTRUCTIONS,
            output_type=None,
            prompt=synthesis_prompt,
            preferred_model=final_model,
            max_turns=4,
            kind="finalizer",
        )
        total_usage = add_usage(total_usage, final_usage)
        add_run_usage(run_id, final_usage)
        final_text = str(final_output)

        if final_status == "completed":
            set_run_status(run_id, "completed", final_summary=final_text)
        else:
            set_run_status(run_id, final_status, final_summary=final_text, stop_reason=stopped_reason)

        useful_lessons = [lesson for lesson in dict.fromkeys(lessons) if len(lesson) >= 12]
        if useful_lessons:
            save_memory(f"Autonomous run #{run_id}", " | ".join(useful_lessons[:5]), category="lesson", importance=4)

        try:
            append_session_turn(session_id, goal, final_text)
        except Exception as session_exc:
            log_event("autonomy.session.persist_failed", {"run_id": run_id, "session_id": session_id, "error": f"{type(session_exc).__name__}: {session_exc}"})

        meta_artifacts = artifact_evidence["artifacts"]
        log_event("autonomy.run.finish", {"run_id": run_id, "status": final_status, "usage": total_usage, "artifacts": len(meta_artifacts), "model": used_final_model})
        _emit(progress_handler, type="finish", run_id=run_id, status=final_status, total_tokens=total_usage["total_tokens"], artifacts=len(meta_artifacts))
        return final_text, total_usage, {"run_id": run_id, "status": final_status, "run": get_run(run_id) or {}, "artifacts": meta_artifacts, "model": used_final_model}

    except Exception as exc:
        set_run_status(run_id, "failed", stop_reason=f"{type(exc).__name__}: {exc}")
        log_event("autonomy.run.failed", {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
        raise
